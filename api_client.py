import requests
from typing import List, Dict, Any
from datetime import datetime, timedelta


class APIError(Exception):
    """API 异常"""
    pass


class FreeFootballAPI:
    """
    API-Football (RapidAPI) 免费版客户端
    绕过季节限制，获取即将进行的比赛
    """
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api-football-v1.p.rapidapi.com"
        self.headers = {
            "x-rapidapi-key": api_key,
            "x-rapidapi-host": "api-football-v1.p.rapidapi.com"
        }
        self.timeout = 10
    
    # ============ 主要方法 ============
    
    def get_upcoming_matches(self, days_ahead: int = 7) -> List[Dict[str, Any]]:
        """
        获取即将进行的比赛（不依赖赛季）
        
        Args:
            days_ahead: 预看天数（默认7天）
        
        Returns:
            比赛列表
        """
        try:
            # 构造日期范围
            today = datetime.utcnow().date()
            from_date = today.isoformat()
            to_date = (today + timedelta(days=days_ahead)).isoformat()
            
            params = {
                "status": "scheduled",  # ✓ 关键：跳过赛季限制
                "from": from_date,
                "to": to_date
            }
            
            response = requests.get(
                f"{self.base_url}/fixtures",
                headers=self.headers,
                params=params,
                timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()
            
            # 检查 API 错误
            if data.get("errors"):
                error_msg = data["errors"]
                raise APIError(f"API Error: {error_msg}")
            
            fixtures = data.get("response", [])
            return self._parse_fixtures(fixtures)
        
        except requests.RequestException as e:
            raise APIError(f"Network error: {str(e)}")
    
    def get_team_stats(self, team_id: int, season: int = 2024) -> Dict[str, Any]:
        """
        获取球队统计（当前赛季）
        
        Args:
            team_id: 球队 ID
            season: 赛季（默认2024）
        
        Returns:
            球队统计数据
        """
        try:
            params = {
                "team": team_id,
                "season": season
            }
            
            response = requests.get(
                f"{self.base_url}/teams/statistics",
                headers=self.headers,
                params=params,
                timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()
            
            if data.get("errors"):
                raise APIError(f"API Error: {data['errors']}")
            
            stats = data.get("response", {})
            return self._parse_team_stats(stats)
        
        except requests.RequestException as e:
            raise APIError(f"Network error: {str(e)}")
    
    def get_head_to_head(self, team1_id: int, team2_id: int, last: int = 10) -> Dict[str, Any]:
        """
        获取两队历史交战记录
        
        Args:
            team1_id: 球队1 ID
            team2_id: 球队2 ID
            last: 最近比赛数（默认10场）
        
        Returns:
            交战记录统计
        """
        try:
            params = {
                "h2h": f"{team1_id}-{team2_id}",
                "last": last
            }
            
            response = requests.get(
                f"{self.base_url}/fixtures",
                headers=self.headers,
                params=params,
                timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()
            
            if data.get("errors"):
                raise APIError(f"API Error: {data['errors']}")
            
            fixtures = data.get("response", [])
            return self._parse_h2h(fixtures, team1_id, team2_id)
        
        except requests.RequestException as e:
            raise APIError(f"Network error: {str(e)}")
    
    # ============ 解析方法 ============
    
    def _parse_fixtures(self, fixtures: List[Dict]) -> List[Dict[str, Any]]:
        """解析比赛数据"""
        matches = []
        
        for fixture in fixtures:
            try:
                match = {
                    'id': fixture['fixture']['id'],
                    'date': fixture['fixture']['date'],
                    'timestamp': fixture['fixture']['timestamp'],
                    'league': fixture['league']['name'],
                    'league_id': fixture['league']['id'],
                    'country': fixture['league'].get('country', 'N/A'),
                    'logo': fixture['league'].get('logo', ''),
                    'home_team': fixture['teams']['home']['name'],
                    'home_id': fixture['teams']['home']['id'],
                    'home_logo': fixture['teams']['home'].get('logo', ''),
                    'away_team': fixture['teams']['away']['name'],
                    'away_id': fixture['teams']['away']['id'],
                    'away_logo': fixture['teams']['away'].get('logo', ''),
                    'status': fixture['fixture']['status']['short'],
                }
                matches.append(match)
            except KeyError as e:
                continue
        
        return matches
    
    def _parse_team_stats(self, stats: Dict) -> Dict[str, Any]:
        """解析球队统计"""
        try:
            fixtures = stats.get('fixtures', {})
            goals = stats.get('goals', {})
            
            total_games = fixtures.get('played', {}).get('total', 1)
            wins = fixtures.get('wins', {}).get('total', 0)
            
            return {
                'team_id': stats.get('team', {}).get('id'),
                'team_name': stats.get('team', {}).get('name'),
                'played': total_games,
                'wins': wins,
                'draws': fixtures.get('draws', {}).get('total', 0),
                'losses': fixtures.get('losses', {}).get('total', 0),
                'goals_for': goals.get('for', {}).get('total', 0),
                'goals_against': goals.get('against', {}).get('total', 0),
                'goal_diff': goals.get('for', {}).get('total', 0) - goals.get('against', {}).get('total', 0),
                'win_rate': round((wins / total_games * 100), 2) if total_games > 0 else 0,
            }
        except Exception as e:
            return {}
    
    def _parse_h2h(self, fixtures: List[Dict], team1_id: int, team2_id: int) -> Dict[str, Any]:
        """解析历史交战"""
        team1_wins = 0
        team2_wins = 0
        draws = 0
        recent_matches = []
        
        for fixture in fixtures:
            try:
                goals_home = fixture['goals']['home']
                goals_away = fixture['goals']['away']
                
                home_id = fixture['teams']['home']['id']
                away_id = fixture['teams']['away']['id']
                
                # 判断胜负
                if goals_home > goals_away:
                    if home_id == team1_id:
                        team1_wins += 1
                    else:
                        team2_wins += 1
                elif goals_away > goals_home:
                    if away_id == team1_id:
                        team1_wins += 1
                    else:
                        team2_wins += 1
                else:
                    draws += 1
                
                # 记录最近比赛
                recent_matches.append({
                    'home': fixture['teams']['home']['name'],
                    'away': fixture['teams']['away']['name'],
                    'result': f"{goals_home}-{goals_away}",
                    'date': fixture['fixture']['date'][:10]
                })
            except KeyError:
                continue
        
        return {
            'total_matches': len(fixtures),
            f'team1_wins': team1_wins,
            f'team2_wins': team2_wins,
            'draws': draws,
            'recent_matches': recent_matches[:5]  # 最近5场
        }
    
    # ============ 工具方法 ============
    
    def validate_connection(self) -> bool:
        """验证 API 连接"""
        try:
            params = {"status": "scheduled", "last": 1}
            response = requests.get(
                f"{self.base_url}/fixtures",
                headers=self.headers,
                params=params,
                timeout=5
            )
            return response.status_code == 200
        except:
            return False