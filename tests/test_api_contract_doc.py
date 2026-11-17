"""接口面不能漂：文档要跟着代码走，插件不能指着不存在的路由。

2026-11-02 的全面审查里抓到三件事，都是"不会报错，只会慢慢坏"的那一类：

1. ``docs/API_CONTRACT.md`` 说 69 条路由，``app.openapi()`` 实际 85 条——**缺了
   13 条**，包括模型用得最多的 ``/symbol/{symbol}/profile`` 和整个样本账本。
   那份文档是 ``scripts/api_contract.py --write`` 生成的，所以真相很简单：
   改了代码没重跑生成器。没有这道检查，生成型文档必然漂。

2. ``TRAPS``（"不存在的路径"表）只列了 5 条，而 ``data/access.log`` 里模型
   实际猜错过 4 条还没被收进去。表的意义是"下次别再猜"，猜过的就要收。

3. 插件的工具表和 API 路由表是两处手写的东西。路由改名而插件没改，工具就
   指向一个不存在的路径——症状是模型收到 404，然后重试。

这个文件把上面三条都变成会红的测试。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service.api.app import app

CONTRACT_DOC = ROOT / "docs" / "API_CONTRACT.md"
PLUGIN_LIB = ROOT / "plugin" / "lib" / "index.js"
RUNBOOK = ROOT / "docs" / "03-runbook.md"


def _live_routes() -> list[tuple[str, str]]:
    """(method, path) for every operation the service serves."""
    spec = app.openapi()
    routes = []
    for path, ops in spec.get("paths", {}).items():
        for method in ops:
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                routes.append((method.upper(), path))
    return sorted(routes)


def _doc_routes() -> list[tuple[str, str]]:
    text = CONTRACT_DOC.read_text(encoding="utf-8")
    rows = re.findall(r"\| `(GET|POST)` \| `([^`]+)` \|", text)
    return sorted(rows)


def test_the_contract_document_lists_exactly_the_live_routes():
    """文档里的路由表与 app.openapi() 完全一致。

    双向比较：少一条是"模型调不到一个存在的接口"，多一条是"模型去调一个
    不存在的接口"——两种都会在运行时变成一次猜测与重试。
    """
    live = _live_routes()
    documented = _doc_routes()
    assert len(documented) == len(set(documented)), "契约文档里有重复行"
    missing = [r for r in live if r not in documented]
    extra = [r for r in documented if r not in live]
    assert not missing, f"契约文档缺这些路由：{missing}"
    assert not extra, f"契约文档多写了这些路由：{extra}"


def test_the_contract_document_says_how_many_routes_it_lists():
    """正文里的条数必须和表里的行数一致。

    这一条单独存在，是因为漂移最早表现为"正文写 69、表里 72 行"——两个数字
    互相矛盾，而没有人会去数表里的行。
    """
    text = CONTRACT_DOC.read_text(encoding="utf-8")
    stated = re.search(r"^(\d+) routes\.$", text, flags=re.MULTILINE)
    assert stated, "契约文档里没有 'N routes.' 这一行"
    assert int(stated.group(1)) == len(_doc_routes()), (
        f"正文说 {stated.group(1)} 条，表里有 {len(_doc_routes())} 行")


def test_the_trap_table_lists_the_paths_a_model_actually_guessed():
    """实际被猜错的路径必须出现在"不存在的路径"表里。

    这四条来自 ``data/access.log``，每一次都是一个 404 加一次重试。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from api_contract import TRAPS

    for guessed in ("GET /data/health", "GET /profile?symbol=",
                    "GET /knowledge?topic=", "GET /quote?symbols="):
        assert guessed in TRAPS, f"{guessed} 被猜错过，却没有进陷阱表"


def _tool_paths() -> list[str]:
    """The paths the plugin's tools call, with template segments normalised."""
    text = PLUGIN_LIB.read_text(encoding="utf-8")
    found = []
    for match in re.finditer(r"request\(\s*[`'\"]([^`'\"]+)", text):
        # `${symbol}` → `{}`; a path with a template must still match the route.
        found.append(re.sub(r"\$\{[^}]*\}", "{}", match.group(1)))
    return sorted(set(found))


def _route_matches(path: str, template: str) -> bool:
    """Does a concrete path satisfy a route template?

    ``/knowledge/analog`` is served by ``/knowledge/{topic}`` — the plugin
    writes the topic into the path, so a plain string compare would call it a
    missing route. The template's segments are filled by anything that is not a
    slash.
    """
    pattern = re.escape(template)
    pattern = re.sub(r"\\\{[^}]*\\\}", r"[^/]+", pattern)
    return re.fullmatch(pattern, path) is not None


def test_every_path_the_plugin_calls_exists():
    """插件指着的每条路径都得真实存在。

    路由改名而插件没改，是这个接口面最常见的一种破法：工具照常被调用，
    服务端回 404，模型以为"这个功能坏了"或者"数据还没有"，然后重试。
    """
    templates = [path for _method, path in _live_routes()]
    missing = [path for path in _tool_paths()
               if not any(_route_matches(path, template) for template in templates)]
    assert not missing, f"插件调用的这些路径不在 app.openapi() 里：{missing}"


def test_the_contract_script_has_a_check_mode():
    """生成型文档必须有一条 CI 能跑的校验命令。

    没有 ``--check``，就没有人会记得重跑 ``--write``；文档就又会漂。
    """
    text = (ROOT / "scripts" / "api_contract.py").read_text(encoding="utf-8")
    assert '"--check"' in text, "api_contract.py 缺 --check 模式"


def _body_section(name: str, text: str) -> str:
    marker = f"### `{name}`"
    if marker not in text:
        return ""
    return text.split(marker, 1)[1].split("\n### ", 1)[0]


def _body_models() -> dict[str, dict]:
    """每个请求体模型名 → 它的 JSON schema。"""
    spec = app.openapi()
    schemas = spec.get("components", {}).get("schemas", {})
    names: dict[str, dict] = {}
    for ops in spec.get("paths", {}).values():
        for op in ops.values():
            schema = ((op.get("requestBody") or {}).get("content", {})
                      .get("application/json", {}).get("schema", {}))
            ref = schema.get("$ref")
            if ref:
                name = ref.rsplit("/", 1)[-1]
                names[name] = schemas.get(name, {})
    return names


def test_every_request_body_field_is_documented():
    """文档要说清每个 body 收哪些字段。

    以前 ``body`` 列只有 ``ProposeBody`` 这种模型名，而正文让调用者"只发
    documented fields"——字段清单在整份文档里一次都没出现过。模型拼错一个
    名字（``target`` 而不是 ``targets``）得到 422，看起来像"这个接口坏了"。
    """
    text = CONTRACT_DOC.read_text(encoding="utf-8")
    models = _body_models()
    assert models, "app.openapi() 里一个请求体都没有？"

    problems = []
    for name, model in models.items():
        section = _body_section(name, text)
        if not section:
            problems.append(f"{name}: 文档里整节缺失")
            continue
        required = set(model.get("required") or ())
        for field in model.get("properties") or {}:
            line = next((row for row in section.splitlines()
                         if row.startswith(f"| `{field}` |")), "")
            if not line:
                problems.append(f"{name}.{field}: 没有字段行")
                continue
            flag = re.search(r"\| (yes|no) \|", line)
            if not flag:
                problems.append(f"{name}.{field}: 没写必填标记")
            elif (flag.group(1) == "yes") != (field in required):
                problems.append(f"{name}.{field}: 必填标记与 schema 不符")
    assert not problems, problems


def test_a_changed_body_field_would_turn_the_check_red():
    """字段目录必须真的在 ``--check`` 比较的那份文本里。

    ``--check`` 是整份文本比对；这一条证明"给某个模型加一个字段"会改变
    ``render()`` 的输出——否则文档里写着字段表，闸门却仍然只盯路径。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from api_contract import collect, render

    rows = collect()
    before = render(rows)
    for row in rows:
        if row.get("fields"):
            row["fields"] = [*row["fields"], {
                "name": "probe_field", "type": "string",
                "required": False, "constraints": "", "doc": "",
            }]
            break
    else:
        raise AssertionError("没有任何请求体字段，这条闸测不到东西")

    assert render(rows) != before


def test_the_contract_says_which_routes_need_the_token():
    """``auth`` 以前算了却从不输出，而按 ``security`` 算出来 91 条全是 false。

    真实情况是 ``/health`` 与看板首页免鉴权、其余 89 条都要口令。调用者看不到
    这个标记，就会在 401 之后去猜"是不是路径写错了"。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from api_contract import collect

    rows = {(r["method"], r["path"]): r["auth"] for r in collect()}
    assert rows[("GET", "/health")] is False
    assert rows[("GET", "/")] is False
    assert rows[("GET", "/account")] is True
    assert rows[("POST", "/decision/propose")] is True

    text = CONTRACT_DOC.read_text(encoding="utf-8")
    assert re.search(r"\| `GET` \| `/health` \| open \|", text)
    assert re.search(r"\| `GET` \| `/account` \| token \|", text)


def test_the_body_catalog_states_the_422_rule_it_relies_on():
    """“多一个字段就 422”的依据是 ``extra="forbid"``。

    措辞必须跟着 schema 走：所有 body 都 forbid 时说 422，有任何一个放松了
    就得说"忽略多余字段"。写死一句话，模型改了之后文档就会反过来教错。
    """
    text = CONTRACT_DOC.read_text(encoding="utf-8")
    strict = all(model.get("additionalProperties") is False
                 for model in _body_models().values())
    assert ("多一个未声明的字段就 422" in text) is strict
    if not strict:
        assert "忽略多余字段" in text


def test_the_check_failure_lists_what_changed():
    """``--check`` 失败时要说清哪一条变了，而不只是"out of date"。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    from api_contract import _diff

    diff = _diff("a\nold line\n", "a\nnew line\n")
    assert any(line == "-old line" for line in diff), diff
    assert any(line == "+new line" for line in diff), diff


def test_the_runbook_tells_you_how_to_keep_the_contract_current():
    """runbook 里要有那条闸门，否则 --check 只是一段没人跑的代码。"""
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "api_contract.py --check" in text, (
        "runbook 里没有写契约文档的校验命令")
    assert "git diff --exit-code" in text, (
        "runbook 里没有把校验写成 git diff --exit-code 这一道闸")


def test_every_tool_the_skill_names_really_exists():
    """技能是模型的入口文档，它点名的工具必须真的在工具表里。

    这一条比"路由 vs 文档"更容易烂：技能里写的是一串名字，改名时编译器一句话
    都不说，模型到运行时才拿到 404，然后开始猜。2026-11-02 的入口表（用户说一
    句外行话 → 调哪几个）就是这么来的，它一旦指错，不知道工具名的用户会先被
    引到一个不存在的接口上。
    """
    source = (ROOT / "plugin" / "lib" / "index.js").read_text(encoding="utf-8")
    source += (ROOT / "plugin" / "lib" / "facade.js").read_text(encoding="utf-8")
    tools = set(re.findall(r"'(quant_[a-z_]+)'", source))
    skill = (ROOT / "plugin" / "skills" / "quant-desk" / "SKILL.md"
             ).read_text(encoding="utf-8")
    named = set(re.findall(r"\bquant_[a-z_]+\b", skill))

    assert named, "SKILL.md 里一个工具都没点名，入口表大概被删了"
    missing = sorted(named - tools)
    assert not missing, f"SKILL.md 点名了工具表里不存在的工具：{missing}"


def test_the_short_term_entry_does_not_route_to_the_screen():
    """短线的入口必须是 pools，不是 screen——这是外行最容易被带错的一步。

    `quant_screen` 的规则里写着 `not at limit`，也就是说它**故意排除**涨停与
    接近涨停的票，而那恰好是 1–2 日交易要看的那批。工具名叫 screen，听起来像
    "选股"，所以一个不懂的人会先调它，然后得到一份和短线无关的动量清单。
    """
    skill = (ROOT / "plugin" / "skills" / "quant-desk" / "SKILL.md"
             ).read_text(encoding="utf-8")
    short = skill.split("## 短线（1–2 日）", 1)
    assert len(short) == 2, "短线段落不见了"
    body = short[1].split("\n## ", 1)[0]
    assert 'quant_query(resource="pools")' in body, "短线流程必须从 pools 开始"
    assert 'quant_query(resource="screen")' not in body, "短线流程里出现 screen 就是在带错路"

    screen_desc = re.search(
        r"'quant_screen',\s*\n\s*'([^']+)'",
        (ROOT / "plugin" / "lib" / "index.js").read_text(encoding="utf-8"))
    assert screen_desc, "找不到 quant_screen 的描述"
    assert "NOT the short-term one" in screen_desc.group(1), (
        "工具描述也要自己说清它不是短线工具——模型常常只看 schema 不看技能")
