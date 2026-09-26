"""Tests for orchestrator/hooks/scrubber.py (MS4CC#117).

Every credential below is synthetic and assembled at runtime, so no literal
secret-shaped string lives in the repo (and push protection stays quiet).
"""

from __future__ import annotations

import random
import string
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

from scrubber import scrub  # noqa: E402

_rng = random.Random(117)
_URLSAFE = string.ascii_letters + string.digits + "_-"


def _rand(n: int, alphabet: str = string.ascii_letters + string.digits) -> str:
    return "".join(_rng.choice(alphabet) for _ in range(n))


def _mixed(n: int) -> str:
    # Guarantee upper, lower and digit so shape-gated patterns apply.
    return "A" + "a" + "7" + _rand(n - 3, _URLSAFE)


POSITIVE = {
    "anthropic": ("sk-" + "ant-" + "api03-" + _rand(90, _URLSAFE), "[REDACTED-ANTHROPIC-KEY]"),
    "openai-proj": ("sk-" + "proj-" + _rand(150, _URLSAFE), "[REDACTED-OPENAI-KEY]"),
    "openai": ("sk-" + _rand(48), "[REDACTED-OPENAI-KEY]"),
    "google": ("AI" + "za" + _rand(35, _URLSAFE), "[REDACTED-GOOGLE-API-KEY]"),
    "aws": ("AK" + "IA" + _rand(16, string.ascii_uppercase + string.digits), "[REDACTED-AWS-ACCESS-KEY]"),
    "github-ghp": ("gh" + "p_" + _rand(36), "[REDACTED-GITHUB-TOKEN]"),
    "github-gho": ("gh" + "o_" + _rand(36), "[REDACTED-GITHUB-TOKEN]"),
    "github-pat": ("github" + "_pat_" + _rand(60, string.ascii_letters + string.digits + "_"), "[REDACTED-GITHUB-PAT]"),
    "slack-xoxp": ("xo" + "xp-" + _rand(40, string.ascii_letters + string.digits + "-"), "[REDACTED-SLACK-TOKEN]"),
    "telegram": (str(_rng.randint(10**8, 10**9)) + ":" + "AA" + _rand(33, _URLSAFE), "[REDACTED-TELEGRAM-BOT-TOKEN]"),
    "jwt": ("ey" + "J" + _rand(20, _URLSAFE) + ".ey" + "J" + _rand(40, _URLSAFE) + "." + _rand(43, _URLSAFE), "[REDACTED-JWT]"),
    "synapse-43": (_mixed(43), "[REDACTED-URLSAFE-TOKEN]"),
    "sanctum": ("7" + "|" + _rand(48), "[REDACTED-API-TOKEN]"),
}


@pytest.mark.parametrize("name", sorted(POSITIVE))
def test_positive_shapes_are_redacted(name: str) -> None:
    secret, placeholder = POSITIVE[name]
    out = scrub(f"config line: {secret} (end)")
    assert secret not in out
    assert placeholder in out


def test_bearer_keeps_scheme_word() -> None:
    tok = _rand(40, _URLSAFE)
    out = scrub(f"Authorization: Bearer {tok}")
    assert tok not in out
    assert "Bearer [REDACTED-BEARER]" in out


@pytest.mark.parametrize("key", ["api_key", "API-KEY", "access_token", "client_secret", "password"])
def test_generic_assignment_keeps_name_redacts_value(key: str) -> None:
    val = _rand(24)
    out = scrub(f'{key} = "{val}"')
    assert val not in out
    assert key in out
    assert "[REDACTED-SECRET]" in out


def test_private_key_full_and_truncated() -> None:
    body = "\\n".join(_rand(64, string.ascii_letters + string.digits + "+/") for _ in range(4))
    header = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"
    full = f"{header}\\n{body}\\n-----END OPENSSH " + "PRIVATE KEY-----"
    assert scrub(full) == "[REDACTED-PRIVATE-KEY]"
    truncated = f"{header}\\n{body}"
    assert body[:64] not in scrub(truncated)


NEGATIVE = [
    "git sha " + "0123456789abcdef" * 2 + "01234567",  # 40 lowercase hex
    "sha256 " + "0123456789abcdef" * 4,  # 64 lowercase hex
    "uuid 3ad7c136-9284-44c2-96c1-e03e93228762",
    "the task-management-framework-overview is here",  # prose with hyphens
    "risk-assessment and desk-research notes",
    "use sk-learn-for-the-classifier-pipeline here",  # anchored sk- prose, no digit
    "the anthropic-sdk-python-client-library docs",
    "timestamp 2026-09-26T19:54:56.994Z and port 11434",
    "a lowercase_only_identifier_that_is_quite_long_ok",  # no upper/digit mix
    "password: [REDACTED-SECRET]",  # already scrubbed: idempotent
    "call ssh -i ~/.ssh/id_ed25519 host",  # a key PATH is not key material
]


@pytest.mark.parametrize("text", NEGATIVE)
def test_negative_text_is_untouched(text: str) -> None:
    assert scrub(text) == text


def test_idempotent() -> None:
    blob = " ".join(s for s, _ in POSITIVE.values())
    once = scrub(blob)
    assert scrub(once) == once


def test_non_string_passthrough() -> None:
    assert scrub(None) is None  # type: ignore[arg-type]


# --- Review round 1 (adversarial QA of 7e0d294) -----------------------------

@pytest.mark.parametrize(
    "key",
    ["HF_TOKEN", "OPENAI_API_KEY", "OLLAMA_API_KEY", "TELEGRAM_BOT_TOKEN", "SYNAPSE_TOKEN", "token", "secret"],
)
def test_env_var_style_assignments(key: str) -> None:
    val = _rand(30)
    out = scrub(f"export {key}={val}")
    assert val not in out
    assert f"{key}=[REDACTED-SECRET]" in out


def test_hf_token_shape() -> None:
    tok = "hf" + "_" + _rand(34)
    out = scrub(f"login with {tok} now")
    assert tok not in out and "[REDACTED-HF-TOKEN]" in out


@pytest.mark.parametrize(
    "code",
    [
        "password = get_password_from_env()",
        "api_key = config.get_api_key_for_provider(name)",
        "access_token = session.refresh_access_token_now()",
        "max_tokens: 4096",
    ],
)
def test_code_is_not_corrupted(code: str) -> None:
    assert scrub(code) == code


def test_prose_after_private_key_header_survives() -> None:
    note = "-----BEGIN OPENSSH " + "PRIVATE KEY----- is the header line and then the body follows in this memory note"
    assert scrub(note) == note


def test_bearer_prose_survives() -> None:
    for text in ["the bearer of bad news", "Bearer authentication/authorization is described here"]:
        assert scrub(text) == text


@pytest.mark.parametrize(
    "blob",
    [
        "sk-x-" * 200_000,
        "anthropic-" * 100_000,
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----" * 1,
        ("-----BEGIN RSA " + "PRIVATE KEY-----\n") * 30_000,
        "a-" * 500_000,
    ],
    ids=["sk-x", "anthropic", "one-header", "repeated-headers", "a-dash"],
)
def test_large_adversarial_inputs_are_fast(blob: str) -> None:
    import time

    t = time.perf_counter()
    scrub(blob)
    assert time.perf_counter() - t < 1.5


def test_fixed_point_on_glued_tokens() -> None:
    for _ in range(2000):
        parts = [s for s, _ in _rng.sample(list(POSITIVE.values()), 3)]
        blob = "".join(parts)
        once = scrub(blob)
        assert scrub(once) == once


def test_private_key_in_cat_n_tool_output() -> None:
    lines = [_rand(70, string.ascii_letters + string.digits + "+/") for _ in range(3)]
    header = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"
    for sep in ("\t", "\u2192"):
        shown = header + "".join(f"\n{i + 2}{sep}{ln}" for i, ln in enumerate(lines)) + "\n6" + sep + "[truncated]"
        out = scrub(shown)
        assert all(ln not in out for ln in lines), sep
        assert "[REDACTED-PRIVATE-KEY]" in out


# --- Review round 2 (adversarial QA of c319f78) -----------------------------

def _key_body(n: int = 4) -> list[str]:
    return [_rand(64, string.ascii_letters + string.digits + "+/") for _ in range(n)]


@pytest.mark.parametrize(
    "sep",
    ["\\n     {i}\\t", "\\r\\n", "\\n{big}\t", "\n{i}\u2192"],
    ids=["repr-escaped-cat-n", "json-crlf", "seven-digit-lineno", "arrow"],
)
def test_truncated_key_separator_shapes(sep: str) -> None:
    body = _key_body()
    header = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"
    text = header + "".join(sep.format(i=i + 2, big=1000000 + i) + ln for i, ln in enumerate(body))
    out = scrub(text)
    assert all(ln not in out for ln in body)


def test_redacted_value_is_removed_everywhere_in_the_text() -> None:
    val = _rand(20)
    text = f"apikey = {val}\nlater we used {val} again and '{val}' once more"
    out = scrub(text)
    assert val not in out
    assert out.count("[REDACTED-SECRET]") == 3


@pytest.mark.parametrize(
    "fmt",
    ["password = {v}.", "my token: {v}.", "password: {a}.{b}.{c}", "OLLAMA_API_KEY={a}.{b}", "secret={a}~{b}"],
)
def test_value_shapes(fmt: str) -> None:
    a, b, c = _rand(12), _rand(12), _rand(12)
    v = _rand(20)
    text = fmt.format(v=v, a=a, b=b, c=c)
    out = scrub(text)
    for part in (v, a, b) if "{v}" in fmt else (a, b):
        assert part not in out, (fmt, out)


def test_sentence_final_period_is_kept() -> None:
    v = _rand(20)
    assert scrub(f"password = {v}.").endswith("[REDACTED-SECRET].")


def test_recall_usage_scrubs_before_truncating(tmp_path, monkeypatch) -> None:
    import json
    import recall_usage

    monkeypatch.setattr(recall_usage, "LOG_PATH", tmp_path / "ru.jsonl")
    tok = "A" + "a" + "7" + _rand(40, _URLSAFE)
    query = "x" * (recall_usage.QUERY_CAP - 19) + " " + tok
    recall_usage.log("test", query, [{"chunk_id": "c"}])
    row = json.loads((tmp_path / "ru.jsonl").read_text().splitlines()[0])
    assert tok[:18] not in row["query"]


def test_plist_style_arrow_assignment() -> None:
    # `plutil -p` output, e.g. a launchd plist's EnvironmentVariables.
    val = _rand(48, string.hexdigits.lower())
    out = scrub(f'    "MINDSTONE_GATEWAY_TOKEN" => "{val}"')
    assert val not in out and '"MINDSTONE_GATEWAY_TOKEN" => "[REDACTED-' in out


def test_url_userinfo_password() -> None:
    pw = _rand(24)
    out = scrub(f"DATABASE_URL: 'postgres://app:{pw}@db.internal:5432/app'")
    assert pw not in out and "postgres://app:[REDACTED-SECRET]@db.internal" in out


def test_bare_high_entropy_value_on_its_own_line() -> None:
    v = "Qz7" + _rand(29)
    out = scrub(f"DATABASE__PASSWORD=***\n{v}\nNEXT_KEY=1")
    assert v not in out and "[REDACTED-HIGH-ENTROPY]" in out


@pytest.mark.parametrize(
    "benign",
    [
        "see https://github.com/R1ngZer0/MindStone/pull/236/files for details",
        "/Users/clintbodungen/Projects/MindStone/extensions/synapse-client/src/monitor.ts",
        "createSynapseReplyCollector and getTotalPendingReplies",
        "model deepseek-v4.1-flash:cloud and nomic-embed-text",
        "commit 0dd0bd43ae98a6719ad5eb108b7a6320178894d0",
        "session 3ad7c136-9284-44c2-96c1-e03e93228762",
    ],
)
def test_bare_rule_leaves_paths_urls_ids_alone(benign: str) -> None:
    assert scrub(benign) == benign


def test_bare_value_between_json_escaped_newlines() -> None:
    v = "Qz7" + _rand(29)
    # repr()'d tool output: newlines are a literal backslash + n
    out = scrub("DATABASE__PASSWORD=***" + "\\n" + v + "\\n" + "NEXT_KEY=1")
    assert v not in out and "[REDACTED-HIGH-ENTROPY]" in out


# --- Cairn's review of 77db1ea (PR #120) ------------------------------------

def _hex(n: int) -> str:
    return _rand(n, "0123456789abcdef")


@pytest.mark.parametrize(
    "fmt",
    [
        "password: {pw}",
        "password = '{pw}'",
        '"password": "{pw}"',
        "DB_PASS={hex12}",
        "mysql -u root --password {pw}",
        "mysql --password={pw}",
        "curl -u admin:{pw} https://x",
        "redis://:{pw}@cache:6379/0",
    ],
)
def test_password_shapes(fmt: str) -> None:
    pw = "Tr0ub4dor&" + _rand(6) + "!9"
    h = _hex(12)
    text = fmt.format(pw=pw, hex12=h)
    out = scrub(text)
    assert pw not in out and h not in out, (fmt, out)


@pytest.mark.parametrize("benign", ["bypass=manual_review", "compass: north_by_west", "the pass: was icy"])
def test_pass_suffix_only_after_separator(benign: str) -> None:
    assert scrub(benign) == benign


@pytest.mark.parametrize(
    "fmt",
    [
        "<key>MINDSTONE_GATEWAY_TOKEN</key>\n\t<string>{h}</string>",
        "mindstone --token {h}",
        '{{\\"token\\": \\"{h}\\"}}',
        "token: `{h}`",
        "Cookie: session={h}",
        "\n{h48}\n",
    ],
)
def test_hex_tokens_in_context(fmt: str) -> None:
    h, h48 = _hex(40), _hex(48)
    text = fmt.format(h=h, h48=h48)
    out = scrub(text)
    assert h not in out and h48 not in out, (fmt, out)


@pytest.mark.parametrize(
    "fmt",
    ["aws_secret_access_key = {s}", "AWS_SECRET_ACCESS_KEY={s}", '"SecretAccessKey": "{s}"'],
)
def test_aws_secret_access_key(fmt: str) -> None:
    s = _rand(20) + "/" + _rand(9) + "+" + _rand(9)
    assert len(s) == 40
    assert s not in scrub(fmt.format(s=s))


@pytest.mark.parametrize(
    "benign",
    [
        "token = self.token_provider.current",
        "api_key = settings.OPENAI_API_KEY",
        "password = user.password_hash",
        "commit 3|0123456789abcdef0123456789abcdef01234567 in git log",
        "getTotalPendingReplies2 and handleRequest404Error and parseISO8601Timestamp",
        "snake_case_name_v2_final and some_long_identifier_2024_rev3",
        "BackfillJob2024Q3PartitionsByCustomer",
        "secret: /Users/someone/Projects/app/config/secrets.yaml",
        "token file: ~/.synapse/agent.token and password: ./local/pw.txt",  # high entropy + digits; only the switch-rate gate spares it
        "sha " + "0123456789abcdef" * 2 + "01234567" + " on its own line\n" + "0123456789abcdef" * 2 + "01234567\n",
    ],
)
def test_review_false_positives_left_alone(benign: str) -> None:
    assert scrub(benign) == benign


def test_env_prefixed_vendor_key() -> None:
    k = "sk-" + "ant-" + "api03-" + _rand(80, _URLSAFE)
    assert k not in scrub(f"export ANTHROPIC_KEY_{k}")
    assert k not in scrub(f"KEY_{k}")


@pytest.mark.parametrize("n", [7, 11])
def test_telegram_id_lengths(n: int) -> None:
    tok = "1" * n + ":" + "AA" + _rand(33, _URLSAFE)
    assert tok not in scrub(f"bot {tok} ok")


@pytest.mark.parametrize(
    "blob",
    ["token" + " " * 40_000, "token=" * 40_000, "ey" + "J" + "a" * 160_000, "password: " * 30_000],
    ids=["token-ws", "token-eq", "jwt-long", "pw-repeat"],
)
def test_review_quadratic_inputs(blob: str) -> None:
    import time

    t = time.perf_counter()
    scrub(blob)
    assert time.perf_counter() - t < 1.5


# --- Cairn's re-review of 778647f -------------------------------------------

@pytest.mark.parametrize(
    "code",
    [
        "interface Login {\n  username: string;\n  password: string;\n}\nconst x = s.substring(1)",
        "def login(username: Optional[str], password: Optional[str]) -> bool: ...",
        "type User {\n  email: String!\n  password: String!\n}",
        "password: {\n  type: String,\n  required: true\n}\nfield is required",
        'schema = {"password": {"type": "string", "required": True}}',
        "password: required\npassword: disabled\npassword: hidden\npassword: boolean",
        "passwd: list[str] | None",
    ],
    ids=["ts", "py-optional", "graphql", "mongoose", "jsonschema", "keywords", "py-union"],
)
def test_type_annotations_and_schema_words_untouched(code: str) -> None:
    assert scrub(code) == code


def test_propagation_never_eats_part_of_a_word() -> None:
    pw = "W1nter!" + _rand(8)
    text = f"password: {pw}\nreused {pw} and prefix{pw}suffix but substring and required stay"
    out = scrub(text)
    assert pw + " and" not in out
    assert "substring" in out and "required" in out


@pytest.mark.parametrize(
    "fmt",
    [
        "mysql -u root -p{pw} db",
        "PASSWORD: 'p@ss {w} w0rd'",
        "X-Gateway-Key: {h48}",
        "aws s3 ls --aws-secret-access-key {aws}",
    ],
)
def test_round2_minor_shapes(fmt: str) -> None:
    pw, w, h48 = "Zq9" + _rand(9), _rand(5), _hex(48)
    aws = _rand(20) + "/" + _rand(19)
    text = fmt.format(pw=pw, w=w, h48=h48, aws=aws)
    out = scrub(text)
    for secret in (pw, h48, aws, f"p@ss {w} w0rd"):
        if secret in text:
            assert secret not in out, (fmt, out)


@pytest.mark.parametrize(
    "benign",
    [
        "Author: " + _hex(40),
        "authentication " + _hex(40),
        "session_id: " + _hex(32),
        "tokenizer_sha256: " + _hex(64),
    ],
)
def test_hex_context_words_are_whole_words(benign: str) -> None:
    assert scrub(benign) == benign


# Loosening-direction boundaries (a mutant that loosens a threshold fails).
def test_kv_value_length_boundary() -> None:
    assert scrub("api_key = " + "Ab1" * 5) == "api_key = " + "Ab1" * 5  # 15 chars: kept
    assert "[REDACTED" in scrub("api_key = " + "Ab1" * 5 + "Z")  # 16 chars: redacted


def test_password_length_boundary() -> None:
    assert scrub("password: a1b2c") == "password: a1b2c"  # 5 chars: kept
    assert "[REDACTED" in scrub("password: a1b2c3")  # 6 chars: redacted


def test_bare_rule_needs_two_digits_and_entropy() -> None:
    one_digit = "QwErTyUiOpAsDfGhJkLz7"  # 21 chars, 1 digit
    assert scrub(f"value {one_digit} end") == f"value {one_digit} end"
    low_entropy = "Ab12" * 6  # digits, mixed case, but entropy 2.0
    assert scrub(f"value {low_entropy} end") == f"value {low_entropy} end"


def test_propagation_respects_token_boundaries() -> None:
    out = scrub("password: temp1234\nsee temp12345678 and temp1234_backup, but temp1234 again")
    assert "temp12345678" in out and "temp1234_backup" in out
    assert "but [REDACTED-SECRET] again" in out


def test_short_passwords_are_propagated_too() -> None:
    out = scrub("password: a1b2c3\nlater a1b2c3 again and xa1b2c3 stays")
    assert out.startswith("password: [REDACTED-SECRET]")
    assert "later [REDACTED-SECRET] again" in out and "xa1b2c3" in out


@pytest.mark.parametrize(
    "fmt",
    ["PGPASSWORD={w}", "export DB_PASS={w}", "password = {w}", "password: Welcome!", "password: Summer!"],
)
def test_letters_only_values_after_equals_are_secrets(fmt: str) -> None:
    w = _rand(12, string.ascii_letters)
    text = fmt.format(w=w)
    out = scrub(text)
    secret = w if "{w}" in fmt else text.split(": ", 1)[1]
    assert secret not in out, (text, out)


def test_graphql_types_after_colon_still_skipped() -> None:
    for t in ["password: String!", "password: UserInput!", "password: string", "password: Optional[str]"]:
        assert scrub(t) == t
