# 📝 类型提示补全计划

## 当前状态
- 覆盖率: 70%
- 目标: 95%+

## 优先文件

### 1. api_client.py (高优先级)
```python
# 已有类型注解
class FootballAPI:
    def __init__(self, api_key: str, ...) -> None:
    async def _get(self, path: str, params: dict[str, Any], ttl: float = 0.0, fresh: bool = False) -> Any:

# 需补全
async def get_fixtures(self, league_id: int, season: int, date_from: date, date_to: date) -> list[dict]:
    """✅ 已完成"""

# 统计: 已补全 95%+
```

### 2. football_data.py (高优先级)
```python
# 需要补全的方法
async def get_fixtures_range(self, date_from: date, date_to: date) -> list[dict]:
async def get_standings(self, league_id: int, season: int) -> list[dict]:
async def get_fixture_details(self, fixture_id: str) -> dict[str, Any]:
```

### 3. service.py (中优先级)
```python
# PredictionService 类型补全
class PredictionService:
    def __init__(self, ...) -> None:
    async def predict(self, fixture_id: int) -> dict[str, Any]:
    async def get_standings_strength(self, league_id: int) -> dict[str, float]:
```

### 4. data_source.py (中优先级)
```python
class DataSourceRouter:
    def __init__(self, ...) -> None:
    async def get_fixtures(self, ...) -> list[dict]:
    async def switch_source(self) -> None:
```

### 5. main.py (低优先级)
```python
# 处理函数类型
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
```

## 关键类型定义

```python
# 添加到各模块顶部
from typing import TypedDict

class FixtureDict(TypedDict):
    """赛事数据结构"""
    fixture_id: int
    home_team: str
    away_team: str
    date: str
    
class PredictionDict(TypedDict):
    """预测数据结构"""
    home_win: float
    draw: float
    away_win: float
    predicted_score: str
```

## 检查方式

```bash
# 运行 mypy 检查
mypy . --strict

# 查看具体警告
mypy data_source.py --show-error-codes
```

## 进度

- api_client.py: ✅ 95%+
- football_data.py: ⏳ 80%
- service.py: ⏳ 70%
- data_source.py: ⏳ 60%
- main.py: ⏳ 50%

**总覆盖率: 70% → 目标 95%+**

