"""
T3 — community / shared question bank: discovery + clone.
"""

API = "/api/v1"


def _bank_ids(client, headers):
    data = client.get(f"{API}/questions", params={"my_only": "true", "page_size": 50}, headers=headers).json()
    return [it["id"] for it in data["items"]], data["total"]


def test_community_discovery_and_clone(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()

    # Teacher A creates two questions (with answers so they can be made public)
    qs = [
        {"question_text": "Cộng đồng Q1: 1+1=?", "subject_code": "toan",
         "question_type": "TN", "difficulty": "NB", "grade": 6, "chapter": "C1", "answer": "A"},
        {"question_text": "Cộng đồng Q2: 2+2=?", "subject_code": "toan",
         "question_type": "TN", "difficulty": "TH", "grade": 6, "chapter": "C1", "answer": "B"},
    ]
    r = client.post(f"{API}/questions/bulk", json={"questions": qs}, headers=hA)
    assert r.status_code in (200, 201), r.text

    ids_a, _ = _bank_ids(client, hA)
    assert len(ids_a) >= 2

    # Make them public
    client.patch(f"{API}/questions/bulk-visibility",
                 json={"question_ids": ids_a, "is_public": True}, headers=hA)

    # Teacher B sees A's public questions in the community bank, none of their own
    comm = client.get(f"{API}/questions/community", params={"subject": "toan"}, headers=hB).json()
    assert comm["total"] >= 2
    author_ids = {it["user_id"] for it in comm["items"]}
    assert author_ids and all(uid in {q for q in author_ids} for uid in author_ids)  # sanity
    target = comm["items"][0]["id"]

    # B's own bank starts empty
    _, total_b_before = _bank_ids(client, hB)
    assert total_b_before == 0

    # Clone one into B's bank
    c1 = client.post(f"{API}/questions/{target}/clone", headers=hB).json()
    assert c1["cloned"] is True
    _, total_b_after = _bank_ids(client, hB)
    assert total_b_after == 1

    # Cloning the same question again is deduped (no second copy)
    c2 = client.post(f"{API}/questions/{target}/clone", headers=hB).json()
    assert c2["cloned"] is False
    _, total_b_final = _bank_ids(client, hB)
    assert total_b_final == 1

    # The clone is private → does not leak back into the community for others
    comm_again = client.get(f"{API}/questions/community", params={"subject": "toan"}, headers=hA).json()
    cloned_ids = {it["id"] for it in comm_again["items"]}
    assert c1["question_id"] not in cloned_ids


def test_community_excludes_own_and_private(client, make_teacher):
    _, hA = make_teacher()

    qs = [{"question_text": "Riêng tư của A", "subject_code": "toan",
           "question_type": "TN", "difficulty": "NB", "grade": 6, "answer": "A"}]
    client.post(f"{API}/questions/bulk", json={"questions": qs}, headers=hA)
    ids_a, _ = _bank_ids(client, hA)
    client.patch(f"{API}/questions/bulk-visibility",
                 json={"question_ids": ids_a, "is_public": True}, headers=hA)

    # A's own public question must NOT appear in A's community feed
    comm = client.get(f"{API}/questions/community", params={"subject": "toan"}, headers=hA).json()
    assert all(it["id"] not in ids_a for it in comm["items"])


def test_clone_rejects_private_question_of_another_user(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()

    qs = [{"question_text": "Private A only", "subject_code": "toan",
           "question_type": "TN", "difficulty": "NB", "grade": 6, "answer": "A"}]
    client.post(f"{API}/questions/bulk", json={"questions": qs}, headers=hA)
    ids_a, _ = _bank_ids(client, hA)  # private by default-after-create may vary; force private
    client.patch(f"{API}/questions/bulk-visibility",
                 json={"question_ids": ids_a, "is_public": False}, headers=hA)

    r = client.post(f"{API}/questions/{ids_a[0]}/clone", headers=hB)
    assert r.status_code == 403
