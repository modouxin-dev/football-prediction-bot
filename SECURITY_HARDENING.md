# 🔐 安全加固方案

## 安全评估和改进计划

### 1️⃣ 认证和授权

#### 当前状态
- ✅ Telegram 内置认证 (通过 bot token)
- ✅ 私钥管理 (通过环境变量)
- ❌ API 缺少认证层

#### 改进方案
```python
# 添加 API Token 认证
from fastapi.security import HTTPBearer, HTTPAuthCredential
from fastapi import Depends, HTTPException

security = HTTPBearer()

async def verify_token(credentials: HTTPAuthCredential = Depends(security)):
    """验证 API Token"""
    token = credentials.credentials
    if token != os.getenv("API_TOKEN"):
        raise HTTPException(status_code=403, detail="Invalid token")
    return token

# 在端点中使用
@app.get("/api/fixtures")
async def get_fixtures(token: str = Depends(verify_token)):
    ...
```

#### 实施步骤
1. 生成 API Token (secure random)
2. 存储在 .env 中
3. 在所有 API 端点添加认证
4. 添加 token 轮换机制

---

### 2️⃣ 数据加密

#### 当前状态
- ✅ HTTPS 支持 (通过 Railway)
- ❌ 数据库密码字段未加密
- ❌ 敏感日志未脱敏

#### 改进方案
```python
# 加密敏感数据
from cryptography.fernet import Fernet

class EncryptedModel:
    """加密模型基类"""
    _cipher = Fernet(os.getenv("ENCRYPTION_KEY").encode())
    
    @classmethod
    def encrypt(cls, value: str) -> str:
        return cls._cipher.encrypt(value.encode()).decode()
    
    @classmethod
    def decrypt(cls, value: str) -> str:
        return cls._cipher.decrypt(value.encode()).decode()

# 在模型中使用
class APIKey(Base):
    __tablename__ = "api_keys"
    
    id = Column(Integer, primary_key=True)
    encrypted_key = Column(String)  # 加密存储
    
    def set_key(self, key: str):
        self.encrypted_key = EncryptedModel.encrypt(key)
    
    def get_key(self) -> str:
        return EncryptedModel.decrypt(self.encrypted_key)
```

#### 实施步骤
1. 生成加密密钥
2. 为敏感字段添加加密/解密方法
3. 加密数据库中的 API Keys
4. 添加日志脱敏

---

### 3️⃣ SQL 注入防护

#### 当前状态
- ✅ 使用 SQLAlchemy ORM (自动防护)
- ✅ Pydantic 验证输入
- ❌ 需要额外的输入验证

#### 改进方案
```python
# 添加输入验证
from pydantic import BaseModel, Field, validator

class PredictionQuery(BaseModel):
    fixture_id: int = Field(..., gt=0, lt=1000000)
    league_id: int = Field(default=39, gt=0, lt=1000000)
    
    @validator('fixture_id', 'league_id')
    def validate_positive(cls, v):
        if v <= 0:
            raise ValueError('必须为正整数')
        return v

# 在路由中使用
@app.get("/api/predictions")
async def get_prediction(query: PredictionQuery):
    ...
```

#### 实施步骤
1. 为所有查询参数添加 Pydantic 验证
2. 使用 SQLAlchemy 参数化查询 (已有)
3. 添加类型检查
4. 日志审计

---

### 4️⃣ CSRF 防护

#### 当前状态
- ✅ REST API (无状态,天然防护)
- ❌ 需要 CORS 严格配置

#### 改进方案
```python
# 严格的 CORS 配置
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "").split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
    max_age=3600,
)
```

#### 实施步骤
1. 配置 ALLOWED_ORIGINS 环境变量
2. 限制 HTTP 方法
3. 添加 SameSite Cookie
4. 实现 CSRF Token (可选)

---

### 5️⃣ 日志审计

#### 当前状态
- ✅ 基础日志记录
- ❌ 敏感信息可能暴露
- ❌ 缺少审计日志

#### 改进方案
```python
# 构建化日志 + 脱敏
import structlog

structlog.configure(
    processors=[
        structlog.processors.JSONRenderer(),
        # 添加脱敏处理器
    ],
    logger_factory=structlog.PrintLoggerFactory(),
)

class SensitiveDataFilter(logging.Filter):
    """日志脱敏"""
    PATTERNS = [
        ('token', r'token["\']:\s*["\']([^"\']+)["\']'),
        ('password', r'password["\']:\s*["\']([^"\']+)["\']'),
    ]
    
    def filter(self, record):
        message = record.getMessage()
        for name, pattern in self.PATTERNS:
            message = re.sub(pattern, f'{name}": "***"', message)
        record.msg = message
        return True
```

#### 实施步骤
1. 添加脱敏过滤器
2. 使用结构化日志
3. 将日志发送到集中式系统
4. 定期审计敏感操作

---

### 6️⃣ 速率限制

#### 当前状态
- ❌ 没有 API 速率限制

#### 改进方案
```python
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

@app.get("/api/fixtures")
@limiter.limit("100/minute")
async def get_fixtures(request: Request):
    ...
```

#### 限制规则
```
公开端点: 100 请求/分钟
认证端点: 1000 请求/分钟
登录: 5 尝试/分钟
```

---

### 7️⃣ 依赖安全

#### 当前状态
- ❌ 需要定期更新依赖

#### 改进方案
```bash
# 定期检查依赖漏洞
pip install safety
safety check

# 自动更新补丁版本
pip install --upgrade-all --only-binary :all:
```

#### 实施步骤
1. 每月运行 safety check
2. 及时更新依赖
3. 定期审计 requirements.txt
4. 使用 dependabot 自动化

---

### 8️⃣ 错误处理

#### 当前状态
- ✅ 基础异常处理
- ❌ 错误信息可能过详细

#### 改进方案
```python
# 隐藏敏感的错误信息
@app.exception_handler(Exception)
async def universal_exception_handler(request: Request, exc: Exception):
    """统一异常处理 - 生产环境隐藏详情"""
    
    # 记录完整的错误日志
    log.error(f"Unhandled exception: {exc}", exc_info=True)
    
    # 返回通用错误给客户端
    if os.getenv("ENV") == "production":
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"}
        )
    else:
        return JSONResponse(
            status_code=500,
            content={"detail": str(exc)}
        )
```

---

## 安全检查清单

### 代码级别
- [ ] 无硬编码密钥
- [ ] 所有输入都验证
- [ ] 所有输出都转义
- [ ] 错误不暴露内部细节
- [ ] 敏感数据加密存储
- [ ] 日志不包含密钥

### 基础设施级别
- [ ] HTTPS 强制
- [ ] 防火墙规则
- [ ] 定期备份
- [ ] 访问控制
- [ ] 网络隔离

### 运维级别
- [ ] 定期安全审计
- [ ] 依赖漏洞扫描
- [ ] 日志监控
- [ ] 入侵检测
- [ ] 事件响应计划

---

## 安全评分

| 项目 | 当前 | 目标 | 改进方案 |
|------|------|------|---------|
| 认证 | 70% | 95% | API Token + JWT |
| 加密 | 60% | 95% | 字段级加密 |
| 输入验证 | 80% | 100% | Pydantic 全覆盖 |
| 日志审计 | 40% | 90% | 结构化日志 + 脱敏 |
| 速率限制 | 0% | 90% | slowapi 集成 |
| 错误处理 | 70% | 95% | 通用异常处理 |

**总体安全评分**: 70/100 → 目标 95/100

---

## 实施时间表

**Week 4**:
- [ ] 添加 API Token 认证
- [ ] 数据加密实施
- [ ] 严格 CORS 配置

**Week 5**:
- [ ] 日志脱敏
- [ ] 速率限制
- [ ] 错误处理规范化

**Week 6**:
- [ ] 安全审计
- [ ] 渗透测试
- [ ] 最终评估

---

**🔐 安全是首要任务**

