# Vendored third-party assets

Committed so the dashboard runs fully offline (no CDN at runtime).

## Chart.js

- Library: Chart.js
- Version: 4.4.9
- Source: https://cdn.jsdelivr.net/npm/chart.js@4.4.9/dist/chart.umd.js
- License: MIT

To refresh:

```
curl -fsSL https://cdn.jsdelivr.net/npm/chart.js@4.4.9/dist/chart.umd.js \
  -o src/bankapp/web/static/vendor/chart.umd.js
```

## chartjs-chart-sankey

Sankey controller for the Overview cash-flow chart. The UMD build auto-registers
its `sankey` controller and `flow` element on load (verified against Chart.js
4.4.9), so no `Chart.register(...)` call is needed — just load it after
`chart.umd.js`.

- Library: chartjs-chart-sankey
- Version: 0.14.4  (pin exactly — 0.x semver)
- Source: https://cdn.jsdelivr.net/npm/chartjs-chart-sankey@0.14.4/dist/chartjs-chart-sankey.min.js
- License: MIT

To refresh:

```
curl -fsSL https://cdn.jsdelivr.net/npm/chartjs-chart-sankey@0.14.4/dist/chartjs-chart-sankey.min.js \
  -o src/bankapp/web/static/vendor/chartjs-chart-sankey.min.js
```
