"""按 API-Football v3 响应结构构造的测试数据。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api_client import flatten_standings

NOW = datetime(2026, 9, 24, 4, 0, tzinfo=timezone.utc)  # 固定“当前时间”，让测试可重复


def standings_response():
    def row(team_id, name, hp, hgf, hga, ap, agf, aga):
        return {
            "rank": 1,
            "team": {"id": team_id, "name": name},
            "home": {"played": hp, "goals": {"for": hgf, "against": hga}},
            "away": {"played": ap, "goals": {"for": agf, "against": aga}},
        }

    rows = [
        row(1, "Alpha FC", 6, 14, 3, 6, 10, 6),  # 强队
        row(2, "Beta & Sons", 6, 9, 7, 6, 6, 9),
        row(3, "Gamma", 6, 7, 8, 6, 4, 11),
        row(4, "Delta", 6, 5, 10, 6, 3, 12),  # 弱队
    ]
    return [{"league": {"id": 39, "name": "Premier League", "season": 2026, "standings": [rows]}}]


def fixture(fid, home_id, home, away_id, away, kickoff, status="NS", rnd="Regular Season - 6"):
    return {
        "fixture": {"id": fid, "date": kickoff.isoformat(),
                    "status": {"short": status}, "venue": {"name": "Main Ground"}},
        "league": {"id": 39, "name": "Premier League", "round": rnd},
        "teams": {"home": {"id": home_id, "name": home}, "away": {"id": away_id, "name": away}},
    }


def odds_response(prices, handicap=-0.5):
    """prices: [(博彩公司, 主, 平, 客)]。故意把非胜平负的盘口放在前面，检验解析没有取“第一个盘口”。

    ``handicap`` 为全场亚洲盘的盘口线（主队视角，负=主队让球）。
    传 None 表示本场不开亚盘——用于测「无盘不推荐」策略。

    ⚠️ 为什么默认要带亚盘：真实数据源实测返回 id=4 Asian Handicap
    （/oddsprobe 对 fixture 1551707，Pro 套餐共 76 个盘口）。
    若测试样本没有亚盘，所有 Prediction 都会因「无盘不推荐」被过滤掉，
    依赖 build_predictions 造数据的既有用例会整体失效——
    那不是被测逻辑坏了，而是样本与真实数据源不一致。
    """
    bookmakers = []
    for i, (name, h, d, a) in enumerate(prices, start=1):
        bets = [
            {"id": 5, "name": "Goals Over/Under", "values": [{"value": "Over 2.5", "odd": "1.90"}]},
        ]
        if handicap is not None:
            # 同一档盘口线在两侧符号相反：Home -0.5 与 Away +0.5 是一回事
            bets.append({
                "id": 4,
                "name": "Asian Handicap",
                "values": [
                    {"value": f"Home {handicap:+.2f}".rstrip("0").rstrip(".")
                              if handicap != 0 else "Home 0",
                     "odd": "1.95"},
                    {"value": f"Away {-handicap:+.2f}".rstrip("0").rstrip(".")
                              if handicap != 0 else "Away 0",
                     "odd": "1.85"},
                ],
            })
        bets.append({
            "id": 1,
            "name": "Match Winner",
            "values": [
                {"value": "Home", "odd": str(h)},
                {"value": "Draw", "odd": str(d)},
                {"value": "Away", "odd": str(a)},
            ],
        })
        bookmakers.append({"id": i, "name": name, "bets": bets})
    return [{"fixture": {"id": 1}, "bookmakers": bookmakers}]


def h2h_matches():
    def m(date, home_id, home, away_id, away, hg, ag):
        return {
            "fixture": {"date": date},
            "teams": {"home": {"id": home_id, "name": home}, "away": {"id": away_id, "name": away}},
            "goals": {"home": hg, "away": ag},
        }

    return [
        m("2025-12-01T15:00:00+00:00", 1, "Alpha FC", 4, "Delta", 3, 0),
        m("2025-04-01T15:00:00+00:00", 4, "Delta", 1, "Alpha FC", 1, 1),
        m("2024-10-01T15:00:00+00:00", 4, "Delta", 1, "Alpha FC", 2, 0),
    ]


class FakeAPI:
    """替身：不联网，按赛季返回预置赛程。"""

    provider = "rapidapi"
    quota_remaining = "88"

    def __init__(self, fixtures_by_season, odds=None, h2h=None, odds_error=None, h2h_error=None, form=None):
        self.fixtures_by_season = fixtures_by_season
        self.odds = odds if odds is not None else odds_response(
            [("Bet A", 1.80, 3.60, 4.50), ("Bet B", 1.85, 3.50, 4.40)])
        self.h2h = h2h if h2h is not None else h2h_matches()
        self.odds_error, self.h2h_error = odds_error, h2h_error
        self.form = form if form is not None else {}  # {team_id: [近期比赛]}
        self.fixture_calls, self.standings_calls, self.odds_calls = [], [], []
        self.form_calls = []

    async def get_fixtures(self, league_id, season, date_from, date_to):
        self.fixture_calls.append(season)
        return self.fixtures_by_season.get(season, [])

    async def get_standings(self, league_id, season):
        self.standings_calls.append(season)
        return flatten_standings(standings_response())

    async def get_odds(self, fixture_id, fresh=False):
        self.odds_calls.append((fixture_id, fresh))
        if self.odds_error:
            raise self.odds_error
        return self.odds

    async def get_h2h(self, home_id, away_id, last=5):
        if self.h2h_error:
            raise self.h2h_error
        return self.h2h

    async def get_team_form(self, team_id, season, last=5):
        """深度分析用：某队近期已完场比赛。默认空 = 模拟“取不到近期数据”。"""
        self.form_calls.append((team_id, season, last))
        return self.form.get(team_id, [])

    async def get_account_status(self):
        return {"subscription": {"plan": "Pro", "active": True}, "requests": {"current": 12, "limit_day": 7500}}


def default_fixtures():
    k = lambda hours: NOW + timedelta(hours=hours)  # noqa: E731
    return [
        fixture(101, 1, "Alpha FC", 4, "Delta", k(30)),  # 窗口内，最晚
        fixture(102, 2, "Beta & Sons", 3, "Gamma", k(5)),  # 窗口内，最早
        fixture(103, 3, "Gamma", 1, "Alpha FC", k(-3), status="FT"),  # 已结束
        fixture(104, 4, "Delta", 2, "Beta & Sons", k(90)),  # 超出 36 小时窗口
    ]
