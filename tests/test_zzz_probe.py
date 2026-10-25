"""临时探针：定位 ttflist 何时被构建。"""
import os


def test_zzz_probe():
    import matplotlib
    import matplotlib.font_manager as fm
    tl = fm.fontManager.ttflist
    has = any("Noto Sans CJK" in f.name for f in tl)
    print(f"[PROBE-RUN] ttflist={len(tl)} hasCJK={has} cachedir={matplotlib.get_cachedir()}")
    import chart
    print(f"[PROBE-RUN] FONT_IN_USE={chart.FONT_IN_USE!r}")
    print(f"[PROBE-RUN] HOME={os.environ.get('HOME')} MPLCONFIGDIR={os.environ.get('MPLCONFIGDIR')}")
    print(f"[PROBE-RUN] chartfile={getattr(chart, '__file__', '?')}")
