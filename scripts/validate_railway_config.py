#!/usr/bin/env python3
"""
Railway 配置智能检测脚本
Quack! 🦆 小鸭子自动诊断系统
"""

import os
import re
import sys
from pathlib import Path

class RailwayValidator:
    def __init__(self):
        self.root = Path(".")
        self.issues = []
        self.success = []
    
    def check_file_exists(self, filename, description):
        """检查文件是否存在"""
        filepath = self.root / filename
        if filepath.exists():
            self.success.append(f"✅ {filename} 存在")
            return True
        else:
            self.issues.append(f"❌ {filename} 缺失 ({description})")
            return False
    
    def check_file_contains(self, filename, keywords, description):
        """检查文件是否包含关键内容"""
        filepath = self.root / filename
        if not filepath.exists():
            return False
        
        content = filepath.read_text()
        missing = [kw for kw in keywords if kw not in content]
        
        if not missing:
            self.success.append(f"✅ {filename} 包含必要配置")
            return True
        else:
            self.issues.append(
                f"❌ {filename} 缺少: {', '.join(missing)}"
            )
            return False
    
    def validate_all(self):
        """运行所有检测"""
        print("\n" + "="*60)
        print("🦆 Railway 配置智能检测系统")
        print("="*60 + "\n")
        
        # 检测 1: railway.toml
        print("📋 检测 1: railway.toml 配置")
        self.check_file_exists(
            "railway.toml", 
            "Railway 部署配置文件"
        )
        if (self.root / "railway.toml").exists():
            self.check_file_contains(
                "railway.toml",
                ["[build]", "[deploy]", "builder", "startCommand"],
                "Railway 核心配置"
            )
        
        # 检测 2: Dockerfile
        print("📋 检测 2: Dockerfile 配置")
        self.check_file_exists(
            "Dockerfile",
            "Docker 容器镜像文件"
        )
        if (self.root / "Dockerfile").exists():
            self.check_file_contains(
                "Dockerfile",
                ["FROM python", "COPY", "pip install", "CMD"],
                "Docker 基础镜像和启动命令"
            )
        
        # 检测 3: .env.example
        print("📋 检测 3: 环境变量配置")
        self.check_file_exists(
            ".env.example",
            "环境变量模板"
        )
        if (self.root / ".env.example").exists():
            self.check_file_contains(
                ".env.example",
                ["DATABASE_PATH", "API_KEY", "LOG_LEVEL"],
                "必需环境变量"
            )
        
        # 检测 4: Python 依赖
        # 依赖分两个文件：requirements.txt 是机器人本体，requirements-web.txt 是
        # Web 看板（PR #36 起分离）。此前这里要求 requirements.txt 同时含
        # fastapi / sqlalchemy / pandas，与实际结构不符：
        #   fastapi    → 属于 Web 看板，已在 requirements-web.txt
        #   sqlalchemy → 只被 models.py 使用，而该文件是无引用的死代码
        #   pandas     → 全仓无任何 import
        # 照旧规则只能通过往 requirements.txt 里塞无用包来"骗过"检查，
        # 所以改为按真实结构分别校验。
        print("📋 检测 4: Python 依赖")
        if self.check_file_exists("requirements.txt", "机器人本体依赖"):
            self.check_file_contains(
                "requirements.txt",
                ["python-telegram-bot", "httpx", "matplotlib"],
                "机器人核心库"
            )
        if self.check_file_exists("requirements-web.txt", "Web 看板依赖"):
            self.check_file_contains(
                "requirements-web.txt",
                ["fastapi", "uvicorn"],
                "Web 核心库"
            )

        # 检测 4b: Dockerfile 必须装全两份依赖。
        # 这是真 bug 的回归防护：早期 Dockerfile 只写
        # `pip install -r requirements-web.txt`，机器人本体依赖没装，
        # 镜像能构建但进程起不来（PR #36 修过）。
        if (self.root / "Dockerfile").exists():
            dockerfile = (self.root / "Dockerfile").read_text()
            # 只看 pip install 行：Dockerfile 里的 `COPY requirements.txt ...`
            # 同样含这些文件名，按整文件匹配会把"只 COPY 没安装"误判为已装。
            pip_lines = [line for line in dockerfile.splitlines()
                         if "pip install" in line]

            def _installed(name: str) -> bool:
                # requirements-web.txt 不是 requirements.txt 的子串，
                # 但反向不成立，所以锚定 `-r <name>` 后接空白/行尾。
                return any(re.search(rf"-r\s+{re.escape(name)}(\s|$)", line)
                           for line in pip_lines)

            installed = {name for name in ("requirements.txt", "requirements-web.txt")
                         if _installed(name)}
            if installed == {"requirements.txt", "requirements-web.txt"}:
                self.success.append("✅ Dockerfile 同时安装本体与 Web 依赖")
            else:
                missing = {"requirements.txt", "requirements-web.txt"} - installed
                self.issues.append(
                    f"❌ Dockerfile 未安装: {', '.join(sorted(missing))}"
                )

        # 检测 5: main.py
        print("📋 检测 5: 启动脚本")
        self.check_file_exists("main.py", "应用入口")
        if (self.root / "main.py").exists():
            self.check_file_contains(
                "main.py",
                ["app", "run"],
                "应用启动逻辑"
            )
        
        # 检测 6: scripts 目录
        print("📋 检测 6: 脚本工具目录")
        scripts_dir = self.root / "scripts"
        if scripts_dir.exists():
            self.success.append("✅ scripts/ 目录存在")
        else:
            self.issues.append("❌ scripts/ 目录缺失")
        
        # 打印结果
        # 必须 return：此前这里只是调用而没返回，validate_all() 恒为 None，
        # 于是 sys.exit(0 if None else 1) 恒为 1 —— 配置全部正确也会判红。
        return self.print_results()
    
    def print_results(self):
        """打印检测结果"""
        print("\n" + "="*60)
        print("📊 检测结果汇总")
        print("="*60 + "\n")
        
        for msg in self.success:
            print(msg)
        
        print()
        
        if self.issues:
            print("⚠️  发现问题:")
            for msg in self.issues:
                print(msg)
            print()
            print("建议修复:")
            print("1. 在 GitHub 网页编辑器创建缺失的配置文件")
            print("2. 复制对应的模板内容")
            print("3. 提交更改，等待自动检测通过")
            return False
        else:
            print("🎉 所有配置完整！")
            print("✅ 可以部署到 Railway")
            return True

if __name__ == "__main__":
    validator = RailwayValidator()
    success = validator.validate_all()
    sys.exit(0 if success else 1)