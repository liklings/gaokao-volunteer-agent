"""
回归测试：确保仓库里当前的种子数据文件（app/seed_data/jiangsu_physics_seed.json）
始终能通过 scripts/validate_seed_data.py 的校验，不产生任何 ERROR。

这不是重新测 validate_seed_data.py 本身的校验逻辑（那部分已经用一份人工构造的
"深度损坏"数据验证过，见项目记录），这里测的是"数据文件本身状态"——防止以后
有人手改种子数据引入结构性错误却没人发现。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from validate_seed_data import validate  # noqa: E402

SEED_DATA_PATH = Path(__file__).resolve().parent.parent / "app" / "seed_data" / "jiangsu_physics_seed.json"


def test_seed_data_file_has_no_validation_errors():
    payload = json.loads(SEED_DATA_PATH.read_text(encoding="utf-8"))
    report = validate(payload)
    assert not report.errors, f"种子数据存在结构性错误，需要先修复：{[str(i) for i in report.errors]}"
