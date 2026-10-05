"""数据库模型定义 (SQLAlchemy ORM)
用于持久化存储预测结果、赛事数据、统计信息等
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

Base = declarative_base()


class Fixture(Base):
    """赛事模型"""
    __tablename__ = "fixtures"

    id = Column(Integer, primary_key=True)
    fixture_id = Column(Integer, unique=True, index=True)
    league_id = Column(Integer, index=True)
    season = Column(Integer)
    home_team_id = Column(Integer)
    away_team_id = Column(Integer)
    home_team = Column(String(100))
    away_team = Column(String(100))
    match_date = Column(DateTime, index=True)
    status = Column(String(50))  # NOT_STARTED, IN_PLAY, FINISHED
    home_goals = Column(Integer, nullable=True)
    away_goals = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Fixture {self.home_team} vs {self.away_team} ({self.match_date})>"


class Prediction(Base):
    """预测结果模型"""
    __tablename__ = "predictions"

    id = Column(Integer, primary_key=True)
    fixture_id = Column(Integer, index=True)
    league_id = Column(Integer)
    home_team = Column(String(100))
    away_team = Column(String(100))
    home_win_prob = Column(Float)  # 主队胜概率
    draw_prob = Column(Float)      # 平手概率
    away_win_prob = Column(Float)  # 客队胜概率
    predicted_home_goals = Column(Float)
    predicted_away_goals = Column(Float)
    predicted_score = Column(String(20))  # "2-1" 格式
    confidence = Column(String(20))       # "低", "中", "中高", "高"
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    actual_home_goals = Column(Integer, nullable=True)
    actual_away_goals = Column(Integer, nullable=True)
    is_correct = Column(Boolean, nullable=True)  # 是否预测正确
    evaluated_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<Prediction {self.fixture_id}: {self.predicted_score}>"


class Standing(Base):
    """积分榜模型"""
    __tablename__ = "standings"

    id = Column(Integer, primary_key=True)
    league_id = Column(Integer, index=True)
    season = Column(Integer)
    team_id = Column(Integer, index=True)
    team_name = Column(String(100), index=True)
    position = Column(Integer)
    played = Column(Integer)
    won = Column(Integer)
    drawn = Column(Integer)
    lost = Column(Integer)
    goals_for = Column(Integer)
    goals_against = Column(Integer)
    goal_difference = Column(Integer)
    points = Column(Integer)
    updated_at = Column(DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f"<Standing {self.position}. {self.team_name} ({self.points}pts)>"


class Statistics(Base):
    """统计信息模型"""
    __tablename__ = "statistics"

    id = Column(Integer, primary_key=True)
    date = Column(DateTime, index=True)
    league_id = Column(Integer, index=True)
    total_predictions = Column(Integer, default=0)
    correct_predictions = Column(Integer, default=0)
    accuracy = Column(Float, default=0.0)
    avg_confidence = Column(Float, default=0.0)
    avg_response_time = Column(Float, default=0.0)  # 毫秒
    cache_hit_rate = Column(Float, default=0.0)
    memory_usage_mb = Column(Float, default=0.0)
    cpu_usage_percent = Column(Float, default=0.0)
    api_requests = Column(Integer, default=0)
    api_errors = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Statistics {self.date.date()}: {self.accuracy:.1%} accuracy>"


class EloRating(Base):
    """ELO评分模型"""
    __tablename__ = "elo_ratings"

    id = Column(Integer, primary_key=True)
    league_id = Column(Integer, index=True)
    team_id = Column(Integer, index=True)
    team_name = Column(String(100))
    elo_rating = Column(Float, default=1600.0)  # 初始ELO评分
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)

    def __repr__(self):
        return f"<EloRating {self.team_name}: {self.elo_rating:.0f}>"


class Cache(Base):
    """缓存模型 (用于持久化缓存)"""
    __tablename__ = "cache"

    id = Column(Integer, primary_key=True)
    key = Column(String(255), unique=True, index=True)
    value = Column(String)  # JSON 序列化的值
    expires_at = Column(DateTime, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    hits = Column(Integer, default=0)

    def __repr__(self):
        return f"<Cache {self.key}>"


# 数据库初始化函数
def init_db(database_url: str = "sqlite:///football.db") -> None:
    """初始化数据库"""
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)


def get_session(database_url: str = "sqlite:///football.db"):
    """获取数据库会话"""
    engine = create_engine(database_url)
    Session = sessionmaker(bind=engine)
    return Session()

