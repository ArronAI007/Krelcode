"""scripts/ 下脚本的静态守护（防语法腐化；真模型验收本身需 API key，不在离线套件内）。"""

import ast
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_real_acceptance_script_parses_and_structured():
    script = ROOT / "scripts" / "real_acceptance.py"
    assert script.is_file()
    tree = ast.parse(script.read_text(encoding="utf-8"))
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    # 7 个阶段函数 + 装配 + 主流程
    for expected in ("stage_chat", "stage_tool_call", "stage_file_ops",
                     "stage_run_command", "stage_context_injection",
                     "stage_session_record", "stage_usage",
                     "build_stack", "run_acceptance", "main"):
        assert expected in names, f"缺少 {expected}"
