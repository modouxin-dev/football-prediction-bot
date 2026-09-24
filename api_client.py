import requests
import os
import logging
from functools import lru_cache
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class APIError(Exception):
    """API 调用异常"""
    pass


class FootballAPI:
    """RapidAPI 足球数据接口封装"""
    
    def __init__(self, cache_ttl_seconds=3600):
        self.api_key = os.getenv("RAPID_API_KEY")
        self.base_url = "https://api-football-v1.p.rapidapi.com/v3"
        self.headers = {
            "X-RapidAPI-Key": self.api_key,
            "X-RapidAPI-Host": "api-football-v1.p.rapidapi.com"
        }
        self.cache_ttl = cache_ttl_seconds
        self._cache = {}

    def _cache_key(self, endpoint, params):
        """生成缓存键"""
        param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        return f"{endpoint}:{param_str}"

    def _is_cache_valid(self, key):
        """检查缓存是否有效"""
        if key not in self._cache:
            return False
        cached_time, _ = self._cache[key]
        return datetime.now() - cached_time < timedelta(seconds=self.cache_ttl)

    def _get_cached(self, key):
        """获取缓存数据"""
        if self._is_cache_valid(key):
            _, data = self._cache[key]
            return data
        return None

    def _set_cache(self, key, data):
        """设置缓存"""
        self._cache[key] = (datetime.now(), data)

    def _request(self, endpoint, params):
        """发送 API 请求，支持缓存"""
        cache_key = self._cache_key(endpoint, params)
        
        # 尝试从缓存获取
        cached_data = self._get_cached(cache_key)
        if cached_data is not None:
            logger.debug(f"Cache hit: {cache_key}")
            return cached_data

        # API 调用
        try:
            url = f"{self.base_url}/{endpoint}"
            response = requests.get(
                url,
                headers=self.headers,
                params=params,
                timeout=10
            )
            response.raise_for_status()
            
            data = response.json()
            
            # 检查 API 错误
            if data.get("errors"):
                raise APIError(f"API returned errors: {data['errors']}")
            
            # 缓存结果
            result = data.get("response", [])
            self._set_cache(cache_key, result)
            
            return result
            
        except requests.exceptions.Timeout:
            logger.error(f"API timeout for {endpoint}")
            raise APIError("API request timeout")
        except requests.exceptions.RequestException as e:
            logger.error(f"API request error: {e}")
            raise APIError(f"API request failed: {str(e)}")

    def get_fixtures(self, league_id, season, status="not_started", next_matches=10):
        """获取赛程"""
        params = {
            "league": league_id,
            "season": season,
            "next": next_matches,
            "status": status
        }
        return self._request("fixtures", params)

    def get_fixture_details(self, fixture_id):
        """获取单场比赛详情"""
        params = {"id": fixture_id}
        data = self._request("fixtures", params)
        return data[0] if data else None

    def get_odds(self, fixture_id, bookmaker=None):
        """获取实时赔率"""
        params = {"fixture": fixture_id}
        if bookmaker:
            params["bookmaker"] = bookmaker
        
        odds_data = self._request("odds", params)
        if odds_data and len(odds_data) > 0:
            return odds_data[0]
        return None

    def get_statistics(self, team_id, league_id, season):
        """获取球队统计数据"""
        params = {
            "team": team_id,
            "league": league_id,
            "season": season
        }
        data = self._request("teams/statistics", params)
        return data[0] if data else {}

    def get_h2h(self, home_team_id, away_team_id, last=10):
        """获取两队历史对阵记录"""
        params = {
            "h2h": f"{home_team_id}-{away_team_id}",
            "last": last
        }
        return self._request("fixtures", params)

    def get_team_info(self, team_id, league_id, season):
        """获取球队信息"""
        params = {
            "id": team_id,
            "league": league_id,
            "season": season
        }
        data = self._request("teams", params)
        return data[0] if data else {}

    def clear_cache(self):
        """清除所有缓存"""
        self._cache.clear()
        logger.info("Cache cleared")

