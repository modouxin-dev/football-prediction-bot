// 在 Vue app 中添加回测页面集成

export default {
  data() {
    return {
      currentView: 'overview', // 添加 'backtesting'
      backtest: {
        selectedSeason: 2024,
        seasons: [2024, 2023, 2022, 2021],
        loading: false,
        results: null,
      },
    }
  },
  
  methods: {
    async runBacktest() {
      this.backtest.loading = true;
      try {
        const response = await axios.get('/api/backtest', {
          params: { season: this.backtest.selectedSeason }
        });
        this.backtest.results = response.data;
      } catch (error) {
        console.error('回测失败', error);
        alert('回测失败,请稍后重试');
      } finally {
        this.backtest.loading = false;
      }
    },
    
    exportBacktestResults() {
      if (!this.backtest.results) return;
      let csv = '比赛,预测比分,实际比分,结果,置信度\n';
      for (const pred of this.backtest.results.predictions) {
        csv += `"${pred.match}",${pred.predicted},${pred.actual},"${pred.correct ? '正确' : '错误'}",${pred.confidence}\n`;
      }
      const blob = new Blob([csv], { type: 'text/csv' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `backtest-${this.backtest.selectedSeason}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    },
  }
}

// 在菜单中添加: <button @click="currentView = 'backtesting'">📊 回测分析</button>

