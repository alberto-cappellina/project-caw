import json
from pathlib import Path

import pytest

from caw.db import connect
from caw.llm import LLMError, extract_json_array
from caw.rules import Rules, load_profile, norm
from caw.score import apply_rules, finalize_verdicts, llm_score, verdict_for

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def profile():
    return load_profile(ROOT / "profile.example.yaml")


@pytest.fixture(scope="module")
def rules(profile):
    return Rules(profile)


def job(**kw):
    base = dict(
        job_id="j1", source="jobleads", title="Senior Android Developer", company="Acme",
        location="Verona", work_mode="hybrid", salary_min=55000, salary_max=65000,
        currency="EUR", contract="full_time", extras="[]", url="u", rank=1,
    )
    base.update(kw)
    return base


def test_good_job_is_ok(rules):
    r = rules.evaluate(job())
    assert r.status == "ok" and r.flags == [] and r.reasons == []


def test_salary_below_minimum_is_excluded(rules, profile):
    below = profile["salary_eur_ral"]["exclude_below"] - 1000
    r = rules.evaluate(job(salary_min=below - 5000, salary_max=below))
    assert r.status == "excluded" and "sotto il minimo" in r.reasons[0]


def test_salary_between_minimum_and_flag_is_flagged_not_excluded(rules, profile):
    sal = profile["salary_eur_ral"]
    r = rules.evaluate(job(salary_min=sal["exclude_below"], salary_max=sal["flag_below"] - 1))
    assert r.status == "ok" and "ral inferiore" in r.flags


def test_salary_uses_max_of_range(rules, profile):
    sal = profile["salary_eur_ral"]  # the minimum is low, but the maximum is above the threshold
    r = rules.evaluate(job(salary_min=sal["exclude_below"] - 5000, salary_max=sal["flag_below"] + 5000))
    assert r.flags == []


def test_missing_salary_is_flagged(rules):
    r = rules.evaluate(job(salary_min=None, salary_max=None))
    assert r.status == "ok" and r.flags == ["RAL non indicata"]


def test_location_outside_province_excluded(rules):
    for mode in ("hybrid", "in_person"):
        r = rules.evaluate(job(location="Vicenza", work_mode=mode))
        assert r.status == "excluded" and "Vicenza" in r.reasons[0]


def test_location_in_province_with_accents_and_suffix(rules):
    assert rules.evaluate(job(location="Legnago")).status == "ok"
    assert rules.evaluate(job(location="Dolcè (VR)")).status == "ok"
    assert rules.evaluate(job(location="San Bonifacio")).status == "ok"


def test_remote_is_ok_anywhere(rules):
    r = rules.evaluate(job(location="Milano", work_mode="remote"))
    assert r.status == "ok" and "full remote" in r.flags


def test_generic_location_needs_verification(rules):
    r = rules.evaluate(job(location="Veneto", work_mode="hybrid"))
    assert r.status == "verify"


def test_remote_in_title_but_onsite_elsewhere_needs_verification(rules):
    r = rules.evaluate(job(title="Remote Flutter Developer", location="Vicenza", work_mode="in_person"))
    assert r.status == "verify"


def test_junior_excluded_unless_senior(rules):
    assert rules.evaluate(job(title="Junior Android Developer")).status == "excluded"
    assert rules.evaluate(job(title="Stage Mobile")).status == "excluded"
    assert rules.evaluate(job(title="Senior Android Developer (mentor for junior)")).status == "ok"


def test_excluded_sector_keyword(rules):
    r = rules.evaluate(job(company="Super Casino Online"))
    assert r.status == "excluded" and "settore escluso" in r.reasons[0]


def test_excluded_wins_over_verify(rules):
    r = rules.evaluate(job(location="Veneto", salary_min=10000, salary_max=20000))
    assert r.status == "excluded"


def test_norm():
    assert norm("Dolcè (VR)") == "dolce vr"


def test_verdict_thresholds(profile):
    assert [verdict_for(s, profile) for s in (95, 70, 69, 40, 39, 0)] == [
        "match", "match", "forse", "forse", "escluso", "escluso",
    ]


def test_extract_json_array():
    assert extract_json_array('ecco:\n```json\n[{"id":"a","score":1}]\n```') == [{"id": "a", "score": 1}]
    with pytest.raises(LLMError):
        extract_json_array("niente")


# ---- pipeline with a fake LLM -----------------------------------------------


class FakeLLM:
    name = "fake"

    def __init__(self, scores=None, fail=False):
        self.scores, self.fail, self.prompts = scores or {}, fail, []

    def complete(self, system, prompt):
        self.prompts.append(prompt)
        if self.fail:
            raise LLMError("boom")
        ids = [json.loads(l)["id"] for l in prompt.splitlines() if l.startswith("{")]
        return json.dumps([{"id": i, "score": self.scores.get(i, 50), "reason": "ok"} for i in ids])


def insert_job(conn, **kw):
    j = job(**kw)
    conn.execute(
        "INSERT INTO jobs (job_id, source, title, company, location, work_mode, salary_min, "
        "salary_max, currency, contract, extras, url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (j["job_id"], j["source"], j["title"], j["company"], j["location"], j["work_mode"],
         j["salary_min"], j["salary_max"], j["currency"], j["contract"], j["extras"], j["url"]),
    )


@pytest.fixture
def conn(tmp_path, rules):
    c = connect(tmp_path / "t.db")
    insert_job(c, job_id="good", title="Senior Android Developer")
    insert_job(c, job_id="far", location="Vicenza", work_mode="in_person")
    insert_job(c, job_id="maybe", title="Mobile Developer", location="Veneto")
    c.commit()
    apply_rules(c, rules)
    return c


def run(conn, profile, llm, **kw):
    return llm_score(conn, profile, llm, "h1", batch_size=2, log=lambda *a: None, **kw)


def test_only_non_excluded_jobs_go_to_llm_and_are_cached(conn, profile):
    llm = FakeLLM({"good": 90, "maybe": 45})
    stats = run(conn, profile, llm)
    assert stats["scored"] == 2 and len(llm.prompts) == 1
    assert '"far"' not in llm.prompts[0]
    assert "dubbi" in llm.prompts[0]  # the 'sede generica' note reaches the LLM
    assert "https" not in llm.prompts[0] and '"url"' not in llm.prompts[0]  # no links

    again = FakeLLM()
    assert run(conn, profile, again)["scored"] == 0 and again.prompts == []  # cache

    finalize_verdicts(conn)
    got = {r["job_id"]: r["verdict"] for r in conn.execute("SELECT * FROM evaluations")}
    assert got == {"good": "match", "maybe": "forse", "far": "escluso"}


def test_profile_change_rescoring(conn, profile):
    run(conn, profile, FakeLLM())
    llm = FakeLLM()
    stats = llm_score(conn, profile, llm, "h2", batch_size=5, log=lambda *a: None)
    assert stats["scored"] == 2


def test_rules_rerun_keeps_scores(conn, profile, rules):
    run(conn, profile, FakeLLM({"good": 88}))
    apply_rules(conn, rules)
    assert conn.execute("SELECT score FROM evaluations WHERE job_id='good'").fetchone()[0] == 88


def test_batch_failure_leaves_jobs_pending(conn, profile):
    stats = run(conn, profile, FakeLLM(fail=True))
    assert stats["batch_errors"] == 1 and stats["scored"] == 0
    finalize_verdicts(conn)
    v = {r["job_id"]: r["verdict"] for r in conn.execute("SELECT * FROM evaluations")}
    assert v["good"] == "da_valutare"
    assert run(conn, profile, FakeLLM())["scored"] == 2  # running again picks them up


def test_missing_profile_gives_a_clear_error(tmp_path):
    with pytest.raises(SystemExit, match="profile.example.yaml"):
        load_profile(tmp_path / "profile.yaml")
