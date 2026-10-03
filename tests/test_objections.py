"""见证人异议（witness objection）端到端测试。

规则要点（见 README「见证人异议」）：
- 仅当前指定见证人可在开封前凭资料编号 + 所见摘要 + 非空理由提出异议；
  非参与方与不存在编号统一 403；摘要不符 409 且不留任何记录；同一资料同时
  至多一件未决异议；
- 受理异议的同一事务内进行中的开封请求失效、旧同意删除，并阻止新开封申请；
  两票已齐且最早开封时刻已到时先按现有规则开封，异议被拒绝且不留记录；
- 异议人可撤回；其余两名当前见证人各凭原摘要驳回一次，重复/摘要不符 409
  不计票，非当前人员（含异议人）403，两票齐才 dismissed 解除拦截；
- 未决期间不得替换异议人；替换其他见证人时旧人驳回票失效；交接后异议
  继续约束新封存人；到期清除照常执行，未决异议 archived 且不可再操作；
- 受理与终结留审计并通知当时参与方；查询异议不泄露正文/所见摘要。
"""
import datetime as dt
import threading
import time

from tests.conftest import auth, register, seal


def in_hours(h):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=h)).isoformat()


def in_days(d):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=d)).isoformat()


def setup_four(client):
    """注册封存人 alice 与三名见证人 bob/carol/dave，封存一份资料。"""
    keys = {n: register(client, n) for n in ["alice", "bob", "carol", "dave", "erin"]}
    body = "异议测试正文 OBJECTION-SECRET"
    resp = seal(client, keys["alice"], body, ["bob", "carol", "dave"])
    return keys, resp["document_id"], resp["digest"], body


def open_request(client, owner_key, doc_id, deadline_hours=24, earliest=None):
    payload = {"reason": "异议测试开封", "deadline": in_hours(deadline_hours)}
    if earliest is not None:
        payload["earliest_open_at"] = earliest
    r = client.post(
        f"/documents/{doc_id}/open-requests", json=payload, headers=auth(owner_key)
    )
    assert r.status_code == 201, r.text
    return r.json()["request_id"]


# ---------------------------------------------------------------------------
# 提出异议：身份/参数校验
# ---------------------------------------------------------------------------


def test_objection_only_current_witness(client):
    keys, doc_id, dg, body = setup_four(client)

    # 封存人不能提出
    r = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "存疑"},
        headers=auth(keys["alice"]),
    )
    assert r.status_code == 403
    # 非参与方/不存在编号统一 403（文案一致，不区分是否存在）
    r = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "存疑"},
        headers=auth(keys["erin"]),
    )
    assert r.status_code == 403
    r = client.post(
        "/documents/999999/objections",
        json={"digest": dg, "reason": "存疑"},
        headers=auth(keys["erin"]),
    )
    assert r.status_code == 403
    # 无/错密钥
    r = client.post(
        f"/documents/{doc_id}/objections", json={"digest": dg, "reason": "存疑"}
    )
    assert r.status_code == 401


def test_objection_reason_required_and_non_blank(client):
    keys, doc_id, dg, _ = setup_four(client)
    r = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": ""},
        headers=auth(keys["bob"]),
    )
    assert r.status_code == 422
    r = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "   \n\t "},
        headers=auth(keys["bob"]),
    )
    assert r.status_code == 422


def test_objection_digest_mismatch_leaves_no_trace(client):
    keys, doc_id, dg, body = setup_four(client)
    r = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": "0" * 64, "reason": "摘要看起来不对"},
        headers=auth(keys["bob"]),
    )
    assert r.status_code == 409
    # 不留记录：审计链无异议相关记录
    audit = client.get(
        f"/documents/{doc_id}/audit", headers=auth(keys["alice"])
    ).json()["records"]
    assert all("objection" not in a["action"] for a in audit)
    # 同一见证人可以立即用正确摘要再提（未被占用未决名额）
    r = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "摘要看起来不对"},
        headers=auth(keys["bob"]),
    )
    assert r.status_code == 201 and r.json()["status"] == "pending"


def test_objection_filed_basic_and_view(client):
    keys, doc_id, dg, body = setup_four(client)
    r = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "我记得的正文摘要不是这个"},
        headers=auth(keys["bob"]),
    )
    assert r.status_code == 201, r.text
    obj = r.json()
    assert obj["document_id"] == doc_id and obj["status"] == "pending"
    assert obj["objector"] == "bob"
    assert obj["dismissal_count"] == 0 and obj["required_dismissals"] == 2
    assert obj["reason"] == "我记得的正文摘要不是这个"
    assert obj["resolved_at"] is None
    # 响应不含正文，也不含提交的所见摘要字段
    assert "body" not in obj and "seen_digest" not in obj and "digest" not in obj
    assert body not in r.text

    # 开封前资料详情呈现未决异议摘要（不含理由）
    doc = client.get(f"/documents/{doc_id}", headers=auth(keys["alice"])).json()
    assert doc["pending_objection"]["objection_id"] == obj["objection_id"]
    assert doc["pending_objection"]["status"] == "pending"
    assert "reason" not in doc["pending_objection"]


def test_only_one_pending_objection_per_document(client):
    keys, doc_id, dg, _ = setup_four(client)
    r = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "bob 的异议"},
        headers=auth(keys["bob"]),
    )
    assert r.status_code == 201
    # 另一名见证人并发/重复提出 -> 409（摘要即使正确）
    r = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "carol 的异议"},
        headers=auth(keys["carol"]),
    )
    assert r.status_code == 409


def test_concurrent_objections_single_pending(client):
    """并发异议：部分唯一索引兜底，至多一件 pending。"""
    keys, doc_id, dg, _ = setup_four(client)
    results = []

    def file_obj(name):
        r = client.post(
            f"/documents/{doc_id}/objections",
            json={"digest": dg, "reason": f"{name} 的并发异议"},
            headers=auth(keys[name]),
        )
        results.append(r.status_code)

    threads = [
        threading.Thread(target=file_obj, args=(n,)) for n in ["bob", "carol", "dave"]
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(201) == 1
    assert sorted(results).count(409) == 2


# ---------------------------------------------------------------------------
# 受理异议：进行中的开封请求失效、旧同意删除、阻止新申请
# ---------------------------------------------------------------------------


def test_objection_invalidates_active_request_and_approvals(client):
    keys, doc_id, dg, _ = setup_four(client)
    req_id = open_request(client, keys["alice"], doc_id)
    # bob 已同意一票
    r = client.post(
        f"/open-requests/{req_id}/approve",
        json={"digest": dg},
        headers=auth(keys["bob"]),
    )
    assert r.status_code == 200
    # carol 提出异议
    r = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "carol 怀疑摘要"},
        headers=auth(keys["carol"]),
    )
    assert r.status_code == 201
    obj_id = r.json()["objection_id"]

    # 进行中的开封请求已 invalidated
    doc = client.get(f"/documents/{doc_id}", headers=auth(keys["alice"])).json()
    assert doc["latest_open_request"]["status"] == "invalidated"
    # 旧同意永久失效：不能再对该请求同意
    r = client.post(
        f"/open-requests/{req_id}/approve",
        json={"digest": dg},
        headers=auth(keys["dave"]),
    )
    assert r.status_code == 409

    # 异议未决期间阻止新的开封申请
    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "再来一次", "deadline": in_hours(24)},
        headers=auth(keys["alice"]),
    )
    assert r.status_code == 409


def test_objection_blocks_new_open_until_resolved(client):
    keys, doc_id, dg, _ = setup_four(client)
    r = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "异议"},
        headers=auth(keys["dave"]),
    )
    assert r.status_code == 201
    assert (
        client.post(
            f"/documents/{doc_id}/open-requests",
            json={"reason": "x", "deadline": in_hours(24)},
            headers=auth(keys["alice"]),
        ).status_code
        == 409
    )


# ---------------------------------------------------------------------------
# 两票驳回解除拦截
# ---------------------------------------------------------------------------


def test_two_dismissals_resolve_objection(client):
    keys, doc_id, dg, _ = setup_four(client)
    r = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "bob 存疑"},
        headers=auth(keys["bob"]),
    )
    obj_id = r.json()["objection_id"]

    # 异议人本人不能驳回
    assert (
        client.post(
            f"/objections/{obj_id}/dismissals",
            json={"digest": dg},
            headers=auth(keys["bob"]),
        ).status_code
        == 403
    )
    # 非参与方/不存在编号统一 403
    assert (
        client.post(
            f"/objections/{obj_id}/dismissals",
            json={"digest": dg},
            headers=auth(keys["erin"]),
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/objections/999999/dismissals",
            json={"digest": dg},
            headers=auth(keys["erin"]),
        ).status_code
        == 403
    )
    # 摘要不符：409 且不计票
    r = client.post(
        f"/objections/{obj_id}/dismissals",
        json={"digest": "f" * 64},
        headers=auth(keys["carol"]),
    )
    assert r.status_code == 409
    view = client.get(
        f"/objections/{obj_id}", headers=auth(keys["alice"])
    ).json()
    assert view["dismissal_count"] == 0 and view["status"] == "pending"

    # carol 第一票
    r = client.post(
        f"/objections/{obj_id}/dismissals",
        json={"digest": dg},
        headers=auth(keys["carol"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["dismissal_count"] == 1 and body["new_status"] == "pending"
    # 一票不足：仍拦截开封
    assert (
        client.post(
            f"/documents/{doc_id}/open-requests",
            json={"reason": "x", "deadline": in_hours(24)},
            headers=auth(keys["alice"]),
        ).status_code
        == 409
    )
    # carol 重复驳回：409 不重复计票
    assert (
        client.post(
            f"/objections/{obj_id}/dismissals",
            json={"digest": dg},
            headers=auth(keys["carol"]),
        ).status_code
        == 409
    )
    # dave 第二票 -> dismissed，解除拦截
    r = client.post(
        f"/objections/{obj_id}/dismissals",
        json={"digest": dg},
        headers=auth(keys["dave"]),
    )
    assert r.status_code == 200
    assert r.json()["new_status"] == "dismissed"
    assert r.json()["status"] == "dismissed"
    assert r.json()["dismissal_count"] == 2

    # 终结后再驳回/撤回 409
    assert (
        client.post(
            f"/objections/{obj_id}/dismissals",
            json={"digest": dg},
            headers=auth(keys["carol"]),
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/objections/{obj_id}/withdraw", headers=auth(keys["bob"])
        ).status_code
        == 409
    )
    # 拦截解除：可以提交新开封请求
    new_req = open_request(client, keys["alice"], doc_id)
    assert new_req != 0


# ---------------------------------------------------------------------------
# 撤回
# ---------------------------------------------------------------------------


def test_objector_can_withdraw(client):
    keys, doc_id, dg, _ = setup_four(client)
    obj_id = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "dave 存疑"},
        headers=auth(keys["dave"]),
    ).json()["objection_id"]

    # 其他人不能撤回（统一 403）
    assert (
        client.post(
            f"/objections/{obj_id}/withdraw", headers=auth(keys["carol"])
        ).status_code
        == 403
    )
    r = client.post(f"/objections/{obj_id}/withdraw", headers=auth(keys["dave"]))
    assert r.status_code == 200 and r.json()["status"] == "withdrawn"
    # 重复撤回 409
    assert (
        client.post(
            f"/objections/{obj_id}/withdraw", headers=auth(keys["dave"])
        ).status_code
        == 409
    )
    # 撤回后解除拦截：可申请开封，且可提出新异议
    open_request(client, keys["alice"], doc_id)
    r = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "dave 再次存疑"},
        headers=auth(keys["dave"]),
    )
    assert r.status_code == 201 and r.json()["status"] == "pending"


def test_dismiss_after_withdraw_rejected(client):
    keys, doc_id, dg, _ = setup_four(client)
    obj_id = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "bob 存疑"},
        headers=auth(keys["bob"]),
    ).json()["objection_id"]
    client.post(
        f"/objections/{obj_id}/dismissals",
        json={"digest": dg},
        headers=auth(keys["carol"]),
    )
    # 一票后异议人撤回：carol 的票保留但异议已终结，dave 不能再驳回
    client.post(f"/objections/{obj_id}/withdraw", headers=auth(keys["bob"]))
    r = client.post(
        f"/objections/{obj_id}/dismissals",
        json={"digest": dg},
        headers=auth(keys["dave"]),
    )
    assert r.status_code == 409
    view = client.get(f"/objections/{obj_id}", headers=auth(keys["alice"])).json()
    assert view["status"] == "withdrawn" and view["dismissal_count"] == 1


# ---------------------------------------------------------------------------
# 未决期间不得替换异议人；替换其他见证人旧票失效
# ---------------------------------------------------------------------------


def test_cannot_replace_objector_while_pending(client):
    keys, doc_id, dg, _ = setup_four(client)
    client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "bob 是异议人"},
        headers=auth(keys["bob"]),
    )
    r = client.post(
        f"/documents/{doc_id}/witness-replacements",
        json={"old_witness": "bob", "new_witness": "erin"},
        headers=auth(keys["alice"]),
    )
    assert r.status_code == 409
    # 名单不变
    doc = client.get(f"/documents/{doc_id}", headers=auth(keys["alice"])).json()
    assert doc["witnesses"] == ["bob", "carol", "dave"]


def test_replace_other_witness_invalidates_dismissal_vote(client):
    keys, doc_id, dg, _ = setup_four(client)
    obj_id = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "dave 是异议人"},
        headers=auth(keys["dave"]),
    ).json()["objection_id"]
    # bob 驳回一票
    r = client.post(
        f"/objections/{obj_id}/dismissals",
        json={"digest": dg},
        headers=auth(keys["bob"]),
    )
    assert r.json()["dismissal_count"] == 1
    # 封存人把 bob 换成 erin：bob 的驳回票失效
    r = client.post(
        f"/documents/{doc_id}/witness-replacements",
        json={"old_witness": "bob", "new_witness": "erin"},
        headers=auth(keys["alice"]),
    )
    assert r.status_code == 200, r.text
    view = client.get(f"/objections/{obj_id}", headers=auth(keys["alice"])).json()
    assert view["status"] == "pending" and view["dismissal_count"] == 0
    # 旧见证人 bob 已不在名单：不能再驳回
    assert (
        client.post(
            f"/objections/{obj_id}/dismissals",
            json={"digest": dg},
            headers=auth(keys["bob"]),
        ).status_code
        == 403
    )
    # 新见证人 erin 可驳回一票；carol 再一票 -> 两票齐 dismissed
    r = client.post(
        f"/objections/{obj_id}/dismissals",
        json={"digest": dg},
        headers=auth(keys["erin"]),
    )
    assert r.status_code == 200 and r.json()["dismissal_count"] == 1
    r = client.post(
        f"/objections/{obj_id}/dismissals",
        json={"digest": dg},
        headers=auth(keys["carol"]),
    )
    assert r.status_code == 200 and r.json()["new_status"] == "dismissed"


def test_replaced_objector_loses_access_but_objection_belongs_to_them(client):
    """异议终结后替换异议人：新人可履职；归档的异议仍记录原异议人。"""
    keys, doc_id, dg, _ = setup_four(client)
    obj_id = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "bob 存疑"},
        headers=auth(keys["bob"]),
    ).json()["objection_id"]
    client.post(
        f"/objections/{obj_id}/dismissals",
        json={"digest": dg},
        headers=auth(keys["carol"]),
    )
    client.post(
        f"/objections/{obj_id}/dismissals",
        json={"digest": dg},
        headers=auth(keys["dave"]),
    )
    # 终结后可替换原异议人
    r = client.post(
        f"/documents/{doc_id}/witness-replacements",
        json={"old_witness": "bob", "new_witness": "erin"},
        headers=auth(keys["alice"]),
    )
    assert r.status_code == 200
    # bob 失去资料访问；但作为异议人仍可查自己的异议
    assert (
        client.get(f"/documents/{doc_id}", headers=auth(keys["bob"])).status_code
        == 403
    )
    view = client.get(f"/objections/{obj_id}", headers=auth(keys["bob"])).json()
    assert view["status"] == "dismissed" and view["objector"] == "bob"


# ---------------------------------------------------------------------------
# 交接后异议继续约束新封存人
# ---------------------------------------------------------------------------


def test_objection_survives_handover(client):
    keys, doc_id, dg, _ = setup_four(client)
    # 先有一个进行中开封请求，carol 提异议使其失效
    req_id = open_request(client, keys["alice"], doc_id)
    client.post(
        f"/open-requests/{req_id}/approve",
        json={"digest": dg},
        headers=auth(keys["bob"]),
    )
    obj_id = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "carol 存疑"},
        headers=auth(keys["carol"]),
    ).json()["objection_id"]

    # 交接给 erin（异议不阻止交接）
    r = client.post(
        f"/documents/{doc_id}/handover-invitations",
        json={"to_username": "erin", "confirm_deadline": in_hours(2)},
        headers=auth(keys["alice"]),
    )
    assert r.status_code == 201, r.text
    inv_id = r.json()["invitation_id"]
    r = client.post(
        f"/handover-invitations/{inv_id}/confirm", headers=auth(keys["erin"])
    )
    assert r.status_code == 200, r.text

    # 新封存人受异议拦截，不能申请开封
    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "新封存人想开", "deadline": in_hours(24)},
        headers=auth(keys["erin"]),
    )
    assert r.status_code == 409
    # 原封存人已无权，新封存人可查异议
    assert (
        client.get(f"/objections/{obj_id}", headers=auth(keys["alice"])).status_code
        == 403
    )
    view = client.get(f"/objections/{obj_id}", headers=auth(keys["erin"])).json()
    assert view["status"] == "pending" and view["objector"] == "carol"
    # 其余当前见证人（bob/dave）两票驳回解除拦截，新封存人随后可申请
    client.post(
        f"/objections/{obj_id}/dismissals",
        json={"digest": dg},
        headers=auth(keys["bob"]),
    )
    r = client.post(
        f"/objections/{obj_id}/dismissals",
        json={"digest": dg},
        headers=auth(keys["dave"]),
    )
    assert r.json()["new_status"] == "dismissed"
    assert open_request(client, keys["erin"], doc_id) > 0


# ---------------------------------------------------------------------------
# 到期清除照常执行：未决异议归档，不可再操作
# ---------------------------------------------------------------------------


def test_purge_archives_pending_objection(client, sweep):
    keys, doc_id, dg, _ = setup_four(client)
    obj_id = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "bob 存疑"},
        headers=auth(keys["bob"]),
    ).json()["objection_id"]
    # carol 先驳回一票
    client.post(
        f"/objections/{obj_id}/dismissals",
        json={"digest": dg},
        headers=auth(keys["carol"]),
    )
    # 安排 1 秒后清除，扫描器结算
    r = client.put(
        f"/documents/{doc_id}/purge",
        json={"purge_at": (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)
        ).isoformat()},
        headers=auth(keys["alice"]),
    )
    assert r.status_code == 200
    time.sleep(1.2)
    sweep()
    # 异议已归档，驳回票保留
    view = client.get(f"/objections/{obj_id}", headers=auth(keys["bob"])).json()
    assert view["status"] == "archived" and view["dismissal_count"] == 1
    assert view["resolved_at"] is not None
    # 归档后不可撤回/驳回
    assert (
        client.post(
            f"/objections/{obj_id}/withdraw", headers=auth(keys["bob"])
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/objections/{obj_id}/dismissals",
            json={"digest": dg},
            headers=auth(keys["dave"]),
        ).status_code
        == 409
    )
    # 已清除资料不能再提异议
    assert (
        client.post(
            f"/documents/{doc_id}/objections",
            json={"digest": dg, "reason": "再提"},
            headers=auth(keys["carol"]),
        ).status_code
        == 409
    )
    # 资料 purged、无正文；审计包含 objection_archived 与 purged
    doc = client.get(f"/documents/{doc_id}", headers=auth(keys["alice"])).json()
    assert doc["status"] == "purged" and "body" not in doc
    records = client.get(
        f"/documents/{doc_id}/audit", headers=auth(keys["alice"])
    ).json()["records"]
    actions = [a["action"] for a in records]
    assert "objection_archived" in actions and "purged" in actions
    assert actions.index("objection_archived") < actions.index("purged")


def test_dismiss_during_due_purge_rejected_after_archive(client, sweep):
    """驳回临界区内清除生效：异议先归档提交，驳回在提交后被拒。"""
    keys, doc_id, dg, _ = setup_four(client)
    obj_id = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "bob 存疑"},
        headers=auth(keys["bob"]),
    ).json()["objection_id"]
    client.put(
        f"/documents/{doc_id}/purge",
        json={"purge_at": (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)
        ).isoformat()},
        headers=auth(keys["alice"]),
    )
    time.sleep(1.2)
    sweep()
    r = client.post(
        f"/objections/{obj_id}/dismissals",
        json={"digest": dg},
        headers=auth(keys["carol"]),
    )
    assert r.status_code == 409
    assert (
        client.get(f"/objections/{obj_id}", headers=auth(keys["alice"])).json()[
            "status"
        ]
        == "archived"
    )


# ---------------------------------------------------------------------------
# 两票已齐且最早开封时刻已到：先开封，异议被拒绝
# ---------------------------------------------------------------------------


def test_objection_rejected_when_waiting_due_opens_first(client, sweep):
    keys, doc_id, dg, body = setup_four(client)
    # 最早开封时刻 1 秒后，截止 24 小时；两票提前集齐 -> waiting
    earliest = (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)
    ).isoformat()
    req_id = open_request(
        client, keys["alice"], doc_id, deadline_hours=24, earliest=earliest
    )
    client.post(
        f"/open-requests/{req_id}/approve",
        json={"digest": dg},
        headers=auth(keys["bob"]),
    )
    client.post(
        f"/open-requests/{req_id}/approve",
        json={"digest": dg},
        headers=auth(keys["carol"]),
    )
    doc = client.get(f"/documents/{doc_id}", headers=auth(keys["alice"])).json()
    assert doc["latest_open_request"]["status"] == "waiting"
    # 等到最早开封时刻；扫描器先开封
    time.sleep(1.2)
    sweep()
    # dave 此刻提出异议：资料已开封 -> 409，不留异议记录
    r = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "太晚了"},
        headers=auth(keys["dave"]),
    )
    assert r.status_code == 409
    doc = client.get(f"/documents/{doc_id}", headers=auth(keys["alice"])).json()
    assert doc["status"] == "opened" and doc["body"] == body
    assert "pending_objection" not in doc
    records = client.get(
        f"/documents/{doc_id}/audit", headers=auth(keys["alice"])
    ).json()["records"]
    assert all("objection" not in a["action"] for a in records)


def test_objection_before_earliest_open_succeeds_even_with_two_votes(client):
    """最早开封时刻未到、两票已齐（waiting）时异议仍可受理并把请求作废。"""
    keys, doc_id, dg, _ = setup_four(client)
    earliest = in_hours(2)
    req_id = open_request(
        client, keys["alice"], doc_id, deadline_hours=24, earliest=earliest
    )
    client.post(
        f"/open-requests/{req_id}/approve",
        json={"digest": dg},
        headers=auth(keys["bob"]),
    )
    r = client.post(
        f"/open-requests/{req_id}/approve",
        json={"digest": dg},
        headers=auth(keys["carol"]),
    )
    assert r.json()["status"] == "waiting"
    # dave 在最早时刻前提出异议：受理，waiting 请求失效
    r = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "dave 在开封前存疑"},
        headers=auth(keys["dave"]),
    )
    assert r.status_code == 201
    doc = client.get(f"/documents/{doc_id}", headers=auth(keys["alice"])).json()
    assert doc["status"] == "sealed"
    assert doc["latest_open_request"]["status"] == "invalidated"


# ---------------------------------------------------------------------------
# 可见性、审计与通知
# ---------------------------------------------------------------------------


def test_objection_visibility(client):
    keys, doc_id, dg, _ = setup_four(client)
    obj_id = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "bob 存疑"},
        headers=auth(keys["bob"]),
    ).json()["objection_id"]
    # 当前参与方都可查
    for k in ["alice", "bob", "carol", "dave"]:
        r = client.get(f"/objections/{obj_id}", headers=auth(keys[k]))
        assert r.status_code == 200
        assert "seen_digest" not in r.json() and "digest" not in r.json()
    # 非参与方与不存在编号统一 403
    assert (
        client.get(f"/objections/{obj_id}", headers=auth(keys["erin"])).status_code
        == 403
    )
    assert (
        client.get("/objections/999999", headers=auth(keys["erin"])).status_code
        == 403
    )


def test_objection_audit_and_notifications(client):
    keys, doc_id, dg, _ = setup_four(client)
    obj_id = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "bob 存疑"},
        headers=auth(keys["bob"]),
    ).json()["objection_id"]
    client.post(
        f"/objections/{obj_id}/dismissals",
        json={"digest": dg},
        headers=auth(keys["carol"]),
    )
    client.post(
        f"/objections/{obj_id}/dismissals",
        json={"digest": dg},
        headers=auth(keys["dave"]),
    )
    # 审计链按顺序记录：objection_filed, request_invalidated?（本场景无请求）,
    # objection_dismissal x2, objection_dismissed
    records = client.get(
        f"/documents/{doc_id}/audit", headers=auth(keys["alice"])
    ).json()["records"]
    actions = [a["action"] for a in records]
    assert actions[0] == "seal"
    assert "objection_filed" in actions
    assert actions.count("objection_dismissal") == 2
    assert actions[-1] == "objection_dismissed"

    # 每名当时参与方都收到 objection_filed 与 objection_dismissed 通知
    for name in ["alice", "bob", "carol", "dave"]:
        notes = client.get("/notifications", headers=auth(keys[name])).json()[
            "notifications"
        ]
        kinds = [n["event_type"] for n in notes if n["document_id"] == doc_id]
        assert "objection_filed" in kinds
        assert "objection_dismissed" in kinds
        # 通知不含理由/正文/摘要
        assert all("reason" not in n and "digest" not in n for n in notes)


def test_objection_on_opened_document_rejected(client):
    keys, doc_id, dg, _ = setup_four(client)
    req_id = open_request(client, keys["alice"], doc_id)
    client.post(
        f"/open-requests/{req_id}/approve",
        json={"digest": dg},
        headers=auth(keys["bob"]),
    )
    client.post(
        f"/open-requests/{req_id}/approve",
        json={"digest": dg},
        headers=auth(keys["carol"]),
    )
    doc = client.get(f"/documents/{doc_id}", headers=auth(keys["alice"])).json()
    assert doc["status"] == "opened"
    r = client.post(
        f"/documents/{doc_id}/objections",
        json={"digest": dg, "reason": "dave 迟来的异议"},
        headers=auth(keys["dave"]),
    )
    assert r.status_code == 409
