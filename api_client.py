import requests
import os
from dotenv import load_dotenv

load_dotenv()

class FootballAPI:
    def __init__(self):
        self.api_key = os.getenv("RAPID_API_KEY")
        self.base_url = "https://api-football-v1.p.rapidapi.com/v3"
        self.headers = {
            "X-RapidAPI-Key": self.api_key,
            "X-RapidAPI-Host": "api-football-v1.p.rapidapi.com"
        }

    def get_fixtures(self, league_id, season):
        """获取赛程"""
        url = f"{self.base_url}/fixtures"
        params = {"league": league_id, "season": season, "next": 10}
        response = requests.get(url, headers=self.headers, params=params)
        return response.json().get('response', [])

    def get_odds(self, fixture_id):
        """获取实时赔率"""
        url = f"{self.base_url}/odds"
        params = {"fixture": fixture_id}
        response = requests.get(url, headers=self.headers, params=params)
        return response.json().get('response', [])

    def get_statistics(self, team_id, league_id, season):
        """获取球队统计数据 (用于计算强度)"""
        url = f"{self.base_url}/teams/statistics"
        params = {"team": team_id, "league": league_id, "season": season}
        response = requests.get(url, headers=self.headers, params=params)
        return response.json().get('response', {})
