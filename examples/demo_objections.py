#!/usr/bin/env python3
"""见证人异议（witness objection）端到端调用示例（纯标准库）。

覆盖本轮全部规则：
1) 开封前当前见证人凭资料编号 + 所见摘要 + 非空理由提出异议，取得异议编号
   与 pending；非参与方与不存在编号统一 403；摘要不符 409 且不留记录；
   同一资料同时至多一件未决异议；
2) 受理异议时进行中的开封请求与旧同意失效，并阻止新开封申请；
3) 异议人撤回；其余两名当前见证人各凭原摘要驳回一次（重复/摘要不符 409
   不计票，异议人本人与非当前人员 403），两票齐 dismissed 解除拦截；
4) 未决期间不得替换异议人；替换其他见证人时旧人的驳回票失效，新见证人
   进入可驳回名单；交接后异议继续约束新封存人；
5) 到期清除照常执行：未决异议随清除 archived，之后不可再操作；
6) 异议受理与终结留审计并通知当时参与方；查询异议不返回正文/所见摘要。

用法: python examples/demo_objections.py [base_url]
"""
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(method, path, key=None, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json",
                 **({"X-API-Key": key} if key else {})},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def in_hours(h):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=h)).isoformat()


def in_seconds(s):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=s)).isoformat()


def show(title, status, body):
    print(f"-- {title} -> {status}")
    print("   " + json.dumps(body, ensure_ascii=False))


def main() -> None:
    users = {}
    for name in ["ob-alice", "ob-bob", "ob-carol", "ob-dave", "ob-erin"]:
        _, body = call("POST", "/register", payload={"username": name})
        users[name] = body["api_key"]
    alice, bob, carol, dave, erin = (
        users[n] for n in ["ob-alice", "ob-bob", "ob-carol", "ob-dave", "ob-erin"]
    )

    # ------------------------------------------------------------------
    # 场景 1：封存资料、提出异议、身份与摘要校验
    # ------------------------------------------------------------------
    print("== 场景 1：提出异议（仅当前见证人、非空理由、摘要须相符）==")
    secret = "异议示例正文：WILL-SECRET-2026"
    _, sealed = call("POST", "/documents", alice,
                     {"body": secret, "witnesses": ["ob-bob", "ob-carol", "ob-dave"]})
    doc_id, digest = sealed["document_id"], sealed["digest"]
    show("封存（响应无正文）", 201, {"document_id": doc_id, "digest": digest[:16] + "…"})

    # 封存人/非参与方不能提出；不存在编号统一 403
    assert call("POST", f"/documents/{doc_id}/objections", alice,
                {"digest": digest, "reason": "封存人不是见证人"})[0] == 403
    assert call("POST", f"/documents/{doc_id}/objections", erin,
                {"digest": digest, "reason": "外人试探"})[0] == 403
    assert call("POST", "/documents/999999/objections", erin,
                {"digest": digest, "reason": "编号试探"})[0] == 403
    # 理由空/纯空白 422；摘要不符 409 且不留记录
    assert call("POST", f"/documents/{doc_id}/objections", bob,
                {"digest": digest, "reason": "   "})[0] == 422
    assert call("POST", f"/documents/{doc_id}/objections", bob,
                {"digest": "0" * 64, "reason": "我记错了摘要"})[0] == 409
    _, audit = call("GET", f"/documents/{doc_id}/audit", alice)
    assert all("objection" not in r["action"] for r in audit["records"]), "摘要不符不得留记录"

    status, obj = call("POST", f"/documents/{doc_id}/objections", bob,
                       {"digest": digest,
                        "reason": "我在封存现场记录的正文摘要与所见不一致"})
    show("bob 提出异议", status, obj)
    assert status == 201 and obj["status"] == "pending"
    assert obj["dismissal_count"] == 0 and obj["required_dismissals"] == 2
    assert "body" not in obj and "digest" not in obj and secret not in json.dumps(obj)
    oid = obj["objection_id"]

    # 同一资料同时至多一件未决异议
    assert call("POST", f"/documents/{doc_id}/objections", carol,
                {"digest": digest, "reason": "我也有异议"})[0] == 409

    # 当前参与方可查异议；外人 403；响应无正文/无摘要
    _, view = call("GET", f"/objections/{oid}", alice)
    show("封存人查询异议（无正文无摘要）", 200, view)
    assert call("GET", f"/objections/{oid}", erin)[0] == 403

    # ------------------------------------------------------------------
    # 场景 2：受理异议使进行中的开封请求与旧同意失效，阻止新申请
    # ------------------------------------------------------------------
    print("== 场景 2：另一份资料演示“受理异议 → 开封请求失效、阻止新申请” ==")
    _, s2 = call("POST", "/documents", alice,
                 {"body": "第二份正文 SECRET-2", "witnesses": ["ob-bob", "ob-carol", "ob-dave"]})
    doc2, dg2 = s2["document_id"], s2["digest"]
    _, req = call("POST", f"/documents/{doc2}/open-requests", alice,
                  {"reason": "家庭会议开封", "deadline": in_hours(24)})
    req2 = req["request_id"]
    call("POST", f"/open-requests/{req2}/approve", bob, {"digest": dg2})
    status, o2 = call("POST", f"/documents/{doc2}/objections", carol,
                      {"digest": dg2, "reason": "carol 对摘要有疑问"})
    show("carol 异议受理（旧请求随即失效）", status, o2)
    assert status == 201
    _, doc2view = call("GET", f"/documents/{doc2}", alice)
    show("资料详情：latest_open_request=invalidated、pending_objection",
         200, {"status": doc2view["status"],
               "latest_open_request": doc2view["latest_open_request"],
               "pending_objection": doc2view["pending_objection"]})
    assert doc2view["latest_open_request"]["status"] == "invalidated"
    # 旧同意作废：其他见证人不能再对失效请求补票
    assert call("POST", f"/open-requests/{req2}/approve", dave,
                {"digest": dg2})[0] == 409
    # 异议未决：封存人不能提交新开封请求
    assert call("POST", f"/documents/{doc2}/open-requests", alice,
                {"reason": "再试一次", "deadline": in_hours(24)})[0] == 409

    # ------------------------------------------------------------------
    # 场景 3：两票驳回解除拦截（重复/摘要不符/异议人本人的规则）
    # ------------------------------------------------------------------
    print("== 场景 3：两名其他当前见证人凭原摘要各驳回一次，两票齐解除 ==")
    # 异议人本人不能驳回（403）；摘要不符 409 且不计票
    assert call("POST", f"/objections/{o2['objection_id']}/dismissals", carol,
                {"digest": dg2})[0] == 403
    assert call("POST", f"/objections/{o2['objection_id']}/dismissals", bob,
                {"digest": "f" * 64})[0] == 409
    status, d1 = call("POST", f"/objections/{o2['objection_id']}/dismissals", bob,
                      {"digest": dg2})
    show("bob 驳回第一票", status, d1)
    assert d1["dismissal_count"] == 1 and d1["new_status"] == "pending"
    # 一票不足，仍拦截开封
    assert call("POST", f"/documents/{doc2}/open-requests", alice,
                {"reason": "x", "deadline": in_hours(24)})[0] == 409
    # 重复驳回只计一次
    assert call("POST", f"/objections/{o2['objection_id']}/dismissals", bob,
                {"digest": dg2})[0] == 409
    status, d2 = call("POST", f"/objections/{o2['objection_id']}/dismissals", dave,
                      {"digest": dg2})
    show("dave 驳回第二票（异议 dismissed）", status, d2)
    assert d2["new_status"] == "dismissed"
    # 拦截解除：可重新申请开封
    status, newreq = call("POST", f"/documents/{doc2}/open-requests", alice,
                          {"reason": "异议解除后重新申请", "deadline": in_hours(24)})
    show("重新提交开封请求成功", status, {"request_id": newreq["request_id"],
                                          "status": newreq["status"]})
    assert status == 201

    # ------------------------------------------------------------------
    # 场景 4：异议人撤回
    # ------------------------------------------------------------------
    print("== 场景 4：异议人撤回，撤回后可重新申请开封 ==")
    status, w = call("POST", f"/objections/{oid}/withdraw", bob)
    show("bob 撤回异议", status, w)
    assert status == 200 and w["status"] == "withdrawn"
    # 其他人不能撤回；已撤回再撤回 409
    assert call("POST", f"/objections/{oid}/withdraw", carol)[0] == 403
    assert call("POST", f"/objections/{oid}/withdraw", bob)[0] == 409
    _, r = call("POST", f"/documents/{doc_id}/open-requests", alice,
                {"reason": "撤回后开封", "deadline": in_hours(24)})
    assert r["status"] == "pending"

    # ------------------------------------------------------------------
    # 场景 5：未决期间不得替换异议人；替换其他见证人旧票失效
    # ------------------------------------------------------------------
    print("== 场景 5：异议人不可替换；替换其他见证人时其驳回票失效 ==")
    _, s3 = call("POST", "/documents", alice,
                 {"body": "第三份正文 SECRET-3", "witnesses": ["ob-bob", "ob-carol", "ob-dave"]})
    doc3, dg3 = s3["document_id"], s3["digest"]
    _, o3 = call("POST", f"/documents/{doc3}/objections", dave,
                 {"digest": dg3, "reason": "dave 存疑"})
    oid3 = o3["objection_id"]
    # 异议人 dave 不能被替换
    assert call("POST", f"/documents/{doc3}/witness-replacements", alice,
                {"old_witness": "ob-dave", "new_witness": "ob-erin"})[0] == 409
    # bob 先驳回一票，再把 bob 换成 erin：旧票失效，erin 可重新驳回
    call("POST", f"/objections/{oid3}/dismissals", bob, {"digest": dg3})
    status, rep = call("POST", f"/documents/{doc3}/witness-replacements", alice,
                       {"old_witness": "ob-bob", "new_witness": "ob-erin"})
    show("替换非异议人 bob -> erin", status, rep)
    _, v3 = call("GET", f"/objections/{oid3}", alice)
    assert v3["status"] == "pending" and v3["dismissal_count"] == 0
    assert call("POST", f"/objections/{oid3}/dismissals", bob,
                {"digest": dg3})[0] == 403  # 旧见证人已非当前人员
    call("POST", f"/objections/{oid3}/dismissals", erin, {"digest": dg3})
    status, d3 = call("POST", f"/objections/{oid3}/dismissals", carol,
                      {"digest": dg3})
    show("erin + carol 两票驳回", status, d3)
    assert d3["new_status"] == "dismissed"

    # ------------------------------------------------------------------
    # 场景 6：交接后异议继续约束新封存人
    # ------------------------------------------------------------------
    print("== 场景 6：交接后异议继续约束新封存人 ==")
    _, s4 = call("POST", "/documents", alice,
                 {"body": "第四份正文 SECRET-4", "witnesses": ["ob-bob", "ob-carol", "ob-dave"]})
    doc4, dg4 = s4["document_id"], s4["digest"]
    _, o4 = call("POST", f"/documents/{doc4}/objections", bob,
                 {"digest": dg4, "reason": "bob 存疑"})
    oid4 = o4["objection_id"]
    _, inv = call("POST", f"/documents/{doc4}/handover-invitations", alice,
                  {"to_username": "ob-erin", "confirm_deadline": in_hours(2)})
    call("POST", f"/handover-invitations/{inv['invitation_id']}/confirm", erin)
    # 新封存人受拦截；原封存人无权查询异议
    assert call("POST", f"/documents/{doc4}/open-requests", erin,
                {"reason": "新主人开封", "deadline": in_hours(24)})[0] == 409
    assert call("GET", f"/objections/{oid4}", alice)[0] == 403
    call("POST", f"/objections/{oid4}/dismissals", carol, {"digest": dg4})
    call("POST", f"/objections/{oid4}/dismissals", dave, {"digest": dg4})
    status, newreq = call("POST", f"/documents/{doc4}/open-requests", erin,
                          {"reason": "驳回齐后新主人申请", "deadline": in_hours(24)})
    show("两票驳回后新封存人申请成功", status, {"request_id": newreq["request_id"]})
    assert status == 201

    # ------------------------------------------------------------------
    # 场景 7：到期清除照常执行，未决异议归档不可再操作
    # ------------------------------------------------------------------
    print("== 场景 7：到期清除照常执行，未决异议 archived ==")
    _, s5 = call("POST", "/documents", alice,
                 {"body": "第五份正文 SECRET-5", "witnesses": ["ob-bob", "ob-carol", "ob-dave"]})
    doc5, dg5 = s5["document_id"], s5["digest"]
    _, o5 = call("POST", f"/documents/{doc5}/objections", bob,
                 {"digest": dg5, "reason": "bob 最后存疑"})
    oid5 = o5["objection_id"]
    call("POST", f"/objections/{oid5}/dismissals", carol, {"digest": dg5})
    call("PUT", f"/documents/{doc5}/purge", alice, {"purge_at": in_seconds(1)})
    print("   等待 1.2 秒让清除时刻到达（扫描器每秒结算）…")
    time.sleep(1.2)
    _, v5 = call("GET", f"/objections/{oid5}", bob)
    show("清除后异议归档（驳回票保留）", 200, v5)
    assert v5["status"] == "archived" and v5["dismissal_count"] == 1
    assert call("POST", f"/objections/{oid5}/withdraw", bob)[0] == 409
    assert call("POST", f"/objections/{oid5}/dismissals", dave,
                {"digest": dg5})[0] == 409
    _, d5 = call("GET", f"/documents/{doc5}", alice)
    show("资料已清除（无正文）", 200, {"status": d5["status"],
                                       "purged_at": d5["purged_at"]})
    assert d5["status"] == "purged" and "body" not in d5
    _, a5 = call("GET", f"/documents/{doc5}/audit", alice)
    assert "objection_archived" in [r["action"] for r in a5["records"]]

    # ------------------------------------------------------------------
    # 场景 8：通知（受理与终结通知当时参与方）
    # ------------------------------------------------------------------
    print("== 场景 8：通知——异议受理/终结通知当时参与方 ==")
    _, notes = call("GET", "/notifications", alice)
    kinds = {n["event_type"] for n in notes["notifications"]}
    show("alice 收到的异议相关通知类型", 200,
         sorted(k for k in kinds if k.startswith("objection")))
    assert {"objection_filed", "objection_dismissed", "objection_archived"} <= kinds
    assert all("reason" not in n and "digest" not in n for n in notes["notifications"])

    print("见证人异议端到端示例全部通过 ✔")


if __name__ == "__main__":
    main()
