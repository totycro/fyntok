"""Plot only SQL-derived totals; currency series are always separate."""
from collections import defaultdict

import plotly.graph_objects as go
from plotly.subplots import make_subplots


def monthly_figure(rows: list[dict]) -> go.Figure:
    figure = make_subplots(rows=2, cols=1, shared_xaxes=True,
                          subplot_titles=('Monthly totals by category and financial bucket', 'Transaction counts'))
    series = defaultdict(list)
    for row in rows:
        series[(row['currency'], row['category_name'], row['bucket'])].append(row)
    for (currency, category, bucket), values in series.items():
        name = f'{category} · {bucket.replace("_", " ")} · {currency}'
        amounts = [r['outgoing_cents'] if bucket == 'spending' else
                   r['incoming_cents'] if bucket == 'income' else r['net_cents'] for r in values]
        figure.add_trace(go.Bar(x=[r['month'] for r in values], y=[v / 100 for v in amounts], name=name), row=1, col=1)
        figure.add_trace(go.Scatter(x=[r['month'] for r in values], y=[r['transaction_count'] for r in values],
                                   name=name, showlegend=False, mode='lines+markers'), row=2, col=1)
    figure.update_layout(height=650, barmode='group', legend=dict(orientation='h'))
    figure.update_yaxes(title_text='Amount (currency in legend)', row=1, col=1)
    figure.update_yaxes(title_text='Transactions', rangemode='tozero', row=2, col=1)
    return figure


def trend_figure(rows: list[dict]) -> go.Figure:
    series = defaultdict(list)
    for row in rows:
        series[(row['currency'], row['category_name'], row['bucket'])].append(row)
    months = sorted({r['month'] for r in rows})
    figure = go.Figure()
    for (currency, category, bucket), values in series.items():
        by_month = {r['month']: r for r in values}
        field = 'outgoing_cents' if bucket == 'spending' else 'incoming_cents' if bucket == 'income' else 'net_cents'
        figure.add_trace(go.Scatter(x=months, y=[by_month[m][field] / 100 if m in by_month else 0 for m in months],
                                   mode='lines', name=f'{category} · {bucket.replace("_", " ")} · {currency}'))
    figure.update_layout(title='Category trends', height=450, yaxis_title='Amount (currency in legend)')
    return figure
