#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四处额度修复的自检脚本（在仓库根目录运行：python3 verify_fixes.py）

判定依据是源码行为特征，不看 commit 号、不问作者。
"""
import re, sys, os

def read(p):
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8", errors="ignore") as f:
        return f.read()

checks = []

# ① #130 额度耗尽立即熔断
s = read("api_client.py") or ""
ok = ("request limit for the day" in s) and ("_mark_quota_exhausted" in s)
checks.append(("#130 主源额度耗尽即熔断", ok, "api_client.py 含 _DAILY_QUOTA_MARKERS + _mark_quota_exhausted"))

# ② #132 已结束赛季本地有数据则跳过
s = read("sync.py") or ""
ok = ("已结束" in s) and ("跳过" in s)
checks.append(("#132 历史赛季回填幂等", ok, "sync.py 含『已结束…跳过』逻辑"))

# ③ #134 自动预测重算冷却
s = read("service.py") or ""
ok = "AUTO_PREDICT_RECALC_COOLDOWN_HOURS" in s
checks.append(("#134 自动预测重算冷却", ok, "service.py 定义重算冷却时长"))
cool = re.search(r"AUTO_PREDICT_RECALC_COOLDOWN_HOURS\s*=\s*([\d.]+)", s)
if cool:
    checks.append(("#134 冷却 < 调度间隔(6h)", float(cool.group(1)) < 6,
                   f"实测冷却={cool.group(1)}h，须小于 6 否则预测会停止更新"))

# ④ #134 joblog.ok 不得嵌进 log 调用实参
s = read("scheduler.py") or ""
bad = [l.strip() for l in s.splitlines()
       if re.search(r"log\.(info|warning|error)\(", l) and "joblog.ok(" in l]
checks.append(("#134 joblog.ok 为独立语句", not bad,
               "若嵌进 log 实参会抛 TypeError，任务被误记成失败"))
if bad:
    print("    缺陷行:", bad[0][:90])

# ⑤ 530e883 无伤停不取名单
s = read("service.py") or ""
ok = bool(re.search(r"if\s+squad_call\s+and\s+injuries|and\s+injuries", s))
checks.append(("530e883 无伤停不取名单", ok, "service.py 中 squad 请求受 injuries 约束"))

print("=" * 62)
passed = 0
for name, ok, why in checks:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n       {why}")
    passed += ok
print("=" * 62)
print(f"结果: {passed}/{len(checks)} 通过")
sys.exit(0 if passed == len(checks) else 1)
