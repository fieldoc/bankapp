# Vendored third-party assets

`chart.umd.js` is Chart.js, committed so the dashboard runs fully offline (no CDN at runtime).

- Library: Chart.js
- Version: 4.4.9
- Source: https://cdn.jsdelivr.net/npm/chart.js@4.4.9/dist/chart.umd.js
- License: MIT

To refresh:

```
curl -fsSL https://cdn.jsdelivr.net/npm/chart.js@4.4.9/dist/chart.umd.js \
  -o src/bankapp/web/static/vendor/chart.umd.js
```
