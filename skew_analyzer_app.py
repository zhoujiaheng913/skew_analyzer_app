import os
import json
import pandas as pd
import numpy as np
from datetime import datetime
import time
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import dash
import dash_bootstrap_components as dbc
from dash import dcc, html, Input, Output, State, MATCH, ALL, ctx

# Shared style settings (applied to inputs in the UI)
INPUT_CLASSNAME = "form-control"
INPUT_STYLE = {"width": "100%"}

# Global cache dictionary to store heavy data based on heavy parameters (e.g. directory, date, ticker, side)
global_heavy_cache = {}

# ------------------------------------------------------------------
# Helper: Diagnose missing data conditions
# ------------------------------------------------------------------
def diagnose_missing_data(directory, start_date, ticker, option_side):
    """
    Checks for various conditions that might cause an empty or invalid dataset:
    - Missing directory
    - No JSON files
    - No files for the given ticker / option side / date range
    Returns a list of error messages (empty if all good).
    """
    errors = []
    if not os.path.exists(directory):
        errors.append("Data directory does not exist.")
        return errors

    json_files = [f for f in os.listdir(directory) if f.endswith('.json')]
    if not json_files:
        errors.append("No JSON files found in the directory.")
        return errors

    files_for_ticker = [f for f in json_files if f.split('_')[0].upper() == ticker.upper()]
    if not files_for_ticker:
        errors.append(f"No files found for ticker '{ticker}'.")
    else:
        files_for_option = []
        for f in files_for_ticker:
            parts = f.split('_')
            if len(parts) >= 4:
                side = parts[-1].replace('.json','')
                if side.upper() == option_side.upper():
                    files_for_option.append(f)
        if not files_for_option:
            errors.append(f"No files found for option side '{option_side}'.")
        else:
            # Verify at least one file has a start date >= requested start_date
            date_ok = False
            for f in files_for_option:
                parts = f.split('_')
                try:
                    file_date = datetime.strptime(parts[1], '%Y-%m-%d')
                    if file_date >= datetime.strptime(start_date, '%Y-%m-%d'):
                        date_ok = True
                        break
                except Exception:
                    continue
            if not date_ok:
                errors.append(f"No files found with start date >= {start_date}.")
    return errors

# ------------------------------------------------------------------
# 1) Heavy Data Loading & Preprocessing Function
# ------------------------------------------------------------------
def load_and_preprocess_data(directory, start_date, ticker, option_side):
    """
    Loads all JSON files matching (ticker, option_side, start_date) from 'directory',
    merges them into a single DataFrame, and applies basic filtering/processing:
    - Timestamps to datetime
    - Calculation of dte, obs_date
    - Adjust underlying for splits if known
    Returns the final processed DataFrame (may be empty).
    """
    if isinstance(start_date, str):
        start_date = datetime.strptime(start_date, '%Y-%m-%d')

    all_dfs = []
    files_count = 0

    # Attempt to list directory contents
    try:
        file_list = os.listdir(directory)
    except Exception as e:
        print(f"Error reading directory: {e}")
        return pd.DataFrame()

    # Identify relevant JSON files
    for file_name in file_list:
        if not file_name.endswith('.json'):
            continue
        parts = file_name.split('_')
        if len(parts) < 4:
            continue
        if parts[0].upper() != ticker.upper():
            continue
        file_side = parts[-1].replace('.json', '').upper()
        if file_side != option_side.upper():
            continue
        try:
            file_start_dt = datetime.strptime(parts[1], '%Y-%m-%d')
        except ValueError:
            continue
        if file_start_dt >= start_date:
            full_path = os.path.join(directory, file_name)
            print(f"Reading file: {file_name}")
            files_count += 1
            try:
                df_temp = pd.read_json(full_path)
                all_dfs.append(df_temp)
            except Exception as e:
                print(f"Error reading file {file_name}: {e}")

    # Merge all data
    if all_dfs:
        df = pd.concat(all_dfs, ignore_index=True)
    else:
        df = pd.DataFrame()

    print(f"Total files read: {files_count}")
    if not df.empty and 'timestamp' in df.columns:
        print("Data timestamp range:", df['timestamp'].min(), df['timestamp'].max())
    else:
        print("No data found for the given criteria.")

    # Filter by currency if present
    if not df.empty and 'currency' in df.columns:
        df = df[df['currency'] == ticker]
    else:
        print("Ticker column not found in data.")

    # Convert timestamps
    for col in ['timestamp', 'expirationTimestamp']:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True, errors='coerce')

    # Filter by putCall if present
    if 'putCall' in df.columns:
        df = df.loc[df['putCall'] == option_side].copy()
    else:
        print("putCall column missing in data.")

    # Compute dte and obs_date if columns exist
    if 'timestamp' in df.columns and 'expirationTimestamp' in df.columns:
        df['dte'] = (df['expirationTimestamp'].dt.floor('D') - df['timestamp'].dt.floor('D')).dt.days
        df['obs_date'] = df['timestamp'].dt.floor('D')

    # Handle known splits if ticker is found in SPLIT_EVENTS
    SPLIT_EVENTS = {
        'MSTR': {'date': '2024-08-08', 'factor': 10},
        # 'AAPL': {'date': '2023-08-01', 'factor': 4}, # etc.
    }

    if not df.empty and 'obs_date' in df.columns and 'underlyingPrice' in df.columns:
        if ticker in SPLIT_EVENTS:
            split_info = SPLIT_EVENTS[ticker]
            split_date_str = split_info['date']
            split_factor = split_info['factor']
            try:
                split_date = datetime.strptime(split_date_str, '%Y-%m-%d').replace(tzinfo=df['obs_date'].dt.tz)
            except ValueError:
                print(f"Warning: Invalid split date for {ticker}: {split_date_str}")
                split_date = None

            if split_date:
                df['adjusted_underlying'] = df['underlyingPrice'].where(
                    df['obs_date'] >= split_date,
                    df['underlyingPrice'] / split_factor
                )
            else:
                df['adjusted_underlying'] = df['underlyingPrice']
        else:
            df['adjusted_underlying'] = df['underlyingPrice']
    else:
        print("Missing obs_date or underlyingPrice columns; cannot apply splits.")

    return df

# ------------------------------------------------------------------
# 2) Compute Skew and Build Figure from Data
# Now with a new parameter 'ma_period' for the moving average window.
# ------------------------------------------------------------------
def compute_skew_figure_from_data(df, rank_days=90, target_dte=25,
                                  base_pct=1.0, compare_pct=1.2, option_side="C", ma_period=5, ticker=""):
    """
    Given a preprocessed DataFrame (df), calculates volatility skew by:
    1) Finding the expiration nearest 'target_dte'
    2) Identifying strikes nearest base_pct and compare_pct moneyness
    3) Computing difference in implied vol (skew) plus a rolling percentile rank
    4) Plotting the results with Plotly
    Returns a Plotly Figure object.
    """
    if df.empty:
        fig = go.Figure()
        fig.add_annotation(text="No available data", showarrow=False, font=dict(size=20))
        return fig

    required = ['timestamp', 'expirationTimestamp', 'underlyingPrice', 'strike', 'markIv', 'dte', 'obs_date', 'adjusted_underlying']
    missing = [col for col in required if col not in df.columns]
    if missing:
        fig = go.Figure()
        msg = f"Missing columns for analysis: {', '.join(missing)}"
        fig.add_annotation(text=msg, showarrow=False, font=dict(size=20))
        return fig

    # Identify best expiration (closest to target DTE)
    df['dte_diff'] = (df['dte'] - target_dte).abs()
    exp_selection = df.groupby(['obs_date', 'expirationTimestamp'], as_index=False)['dte_diff'].first()
    if exp_selection.empty:
        fig = go.Figure()
        fig.add_annotation(text="No available data", showarrow=False, font=dict(size=20))
        return fig
    best_exp = exp_selection.loc[exp_selection.groupby('obs_date')['dte_diff'].idxmin()]
    merged_df = df.merge(best_exp[['obs_date', 'expirationTimestamp']], on=['obs_date', 'expirationTimestamp'], how='inner')
    if merged_df.empty:
        fig = go.Figure()
        fig.add_annotation(text="No available data", showarrow=False, font=dict(size=20))
        return fig

    # Helper function to compute skew for a single day
    def compute_skew_for_day(group):
        underlying = group['underlyingPrice'].iloc[0]
        adjusted_underlying = group['adjusted_underlying'].iloc[0]
        base_strike_target = underlying * base_pct
        compare_strike_target = underlying * compare_pct

        group['abs_diff_base'] = (group['strike'] - base_strike_target).abs()
        group['abs_diff_compare'] = (group['strike'] - compare_strike_target).abs()

        base_row = group.loc[group['abs_diff_base'].idxmin()]
        compare_row = group.loc[group['abs_diff_compare'].idxmin()]
        skew_value = compare_row['markIv'] - base_row['markIv']

        return pd.Series({
            'underlying': underlying,
            'adjusted_underlying': adjusted_underlying,
            'base_strike': base_row['strike'],
            'compare_strike': compare_row['strike'],
            'base_iv': base_row['markIv'],
            'compare_iv': compare_row['markIv'],
            'skew': skew_value
        })

    # Compute daily skew, then add rolling rank + rolling MA
    skew_results = merged_df.groupby('obs_date').apply(compute_skew_for_day).reset_index()
    if skew_results.empty:
        fig = go.Figure()
        fig.add_annotation(text="No available data", showarrow=False, font=dict(size=20))
        return fig

    skew_results.sort_values('obs_date', inplace=True)
    skew_results.set_index('obs_date', inplace=True)

    def percentile_rank(window):
        # Rolling percentile rank calculation
        return (window <= window.iloc[-1]).mean() if not window.isnull().all() else np.nan

    skew_results['skew_rank'] = skew_results['skew'].rolling(window=rank_days, min_periods=1).apply(percentile_rank, raw=False)
    skew_results['skew_ma'] = skew_results['skew'].rolling(window=ma_period, min_periods=1).mean()
    skew_results.reset_index(inplace=True)

    # Build the figure using subplots (two rows: top for skew, bottom for percentile rank)
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.7, 0.3],
        subplot_titles=(
            f"{compare_pct}x vs {base_pct}x Volatility Skew & Price History",
            "Historical Percentile Rank"
        ),
        specs=[[{"secondary_y": True}], [{}]]
    )

    # Skew (raw + MA) and Underlying Price
    fig.add_trace(
        go.Scatter(
            x=skew_results['obs_date'], y=skew_results['skew'],
            mode='lines+markers', name='Skew', line=dict(color='blue'),
            marker=dict(size=5)
        ),
        row=1, col=1, secondary_y=False
    )
    fig.add_trace(
        go.Scatter(
            x=skew_results['obs_date'], y=skew_results['skew_ma'],
            mode='lines', name=f'Skew MA ({ma_period}d)', line=dict(color='blue', dash='dash')
        ),
        row=1, col=1, secondary_y=False
    )
    fig.add_trace(
        go.Scatter(
            x=skew_results['obs_date'], y=skew_results['adjusted_underlying'],
            mode='lines', name='Underlying Price', line=dict(color='green')
        ),
        row=1, col=1, secondary_y=True
    )

    # Skew Rank (row 2)
    fig.add_trace(
        go.Scatter(
            x=skew_results['obs_date'], y=skew_results['skew_rank'],
            mode='lines+markers', name='Skew Rank', line=dict(color='orange'),
            marker=dict(size=5)
        ),
        row=2, col=1, secondary_y=False
    )

    # Horizontal lines to highlight key percentile levels
    fig.add_hline(y=0.8, line_dash="dash", line_color="red",
                  annotation_text="80th %", annotation_position="top right", row=2, col=1)
    fig.add_hline(y=0.5, line_dash="dash", line_color="red",
                  annotation_text="50th %", annotation_position="bottom right", row=2, col=1)
    fig.add_hline(y=0.2, line_dash="dash", line_color="red",
                  annotation_text="20th %", annotation_position="bottom right", row=2, col=1)

    # Configure axes
    fig.update_xaxes(showgrid=True, gridwidth=1, gridcolor='lightgray', row=1, col=1)
    fig.update_xaxes(showgrid=True, gridwidth=1, gridcolor='lightgray', row=2, col=1)
    fig.update_yaxes(showgrid=True, gridwidth=1, gridcolor='lightgray', row=1, col=1, secondary_y=False)
    fig.update_yaxes(showgrid=True, gridwidth=1, gridcolor='lightgray', row=1, col=1, secondary_y=True)
    fig.update_yaxes(showgrid=True, gridwidth=1, gridcolor='lightgray', row=2, col=1)

    # Axis titles
    fig.update_yaxes(title_text="Volatility Skew", row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Underlying (Split-Adjusted)", row=1, col=1, secondary_y=True)
    fig.update_yaxes(title_text="Percentile", row=2, col=1)
    fig.update_xaxes(title_text="Date", row=2, col=1)

    # Layout / legend
    fig.update_layout(
        height=600,
        title=f"Volatility Skew Analysis - {ticker} ({option_side}, {target_dte}D DTE, {base_pct}x vs {compare_pct}x) ({rank_days}-day percentile rank)",
        hovermode="x unified",
        legend=dict(
            x=1.05,
            y=1,
            xanchor='left',
            yanchor='top'
        ),
        paper_bgcolor='white',
        plot_bgcolor='white',
        margin=dict(l=40, r=150, t=80, b=40)
    )

    return fig

# ------------------------------------------------------------------
# Function to Create an Analysis Card
# ------------------------------------------------------------------
def create_analysis_card(index):
    """
    Creates a Dash card (UI) for one analysis plot, including all input parameters
    on the left and the figure (plus message) on the right.
    """
    return dbc.Card(
        id={'type': 'analysis-card', 'index': index},
        className="mb-4",
        children=[
            dbc.CardHeader(
                html.H4(f"Plot {index+1}", className="mb-0"),
                style={"backgroundColor": "#f8f9fa"}
            ),
            dbc.CardBody([
                dbc.Row([
                    # Left Column: user inputs
                    dbc.Col([
                        # Directory
                        dbc.Row([
                            dbc.Col(dbc.Label("Data Directory:", className="fw-bold"), width=4),
                            dbc.Col(
                                dcc.Input(
                                    id={'type': 'directory-input', 'index': index},
                                    type='text',
                                    value='./data',
                                    className=INPUT_CLASSNAME,
                                    style=INPUT_STYLE
                                ),
                                width=8
                            )
                        ], className="mb-2"),

                        # Start Date
                        dbc.Row([
                            dbc.Col(dbc.Label("Start Date:", className="fw-bold"), width=4),
                            dbc.Col(
                                dbc.Input(
                                    id={'type': 'start-date-input', 'index': index},
                                    type='date',
                                    value='2024-02-04',
                                    className=INPUT_CLASSNAME,
                                    style=INPUT_STYLE
                                ),
                                width=8
                            )
                        ], className="mb-2"),

                        # Ticker
                        dbc.Row([
                            dbc.Col(dbc.Label("Ticker:", className="fw-bold"), width=4),
                            dbc.Col(
                                dcc.Input(
                                    id={'type': 'ticker-input', 'index': index},
                                    type='text',
                                    value='MSTR',
                                    className=INPUT_CLASSNAME,
                                    style=INPUT_STYLE
                                ),
                                width=8
                            )
                        ], className="mb-2"),

                        # Option Side
                        dbc.Row([
                            dbc.Col(dbc.Label("Option Side:", className="fw-bold"), width=4),
                            dbc.Col(
                                dbc.Select(
                                    id={'type': 'option-side-select', 'index': index},
                                    options=[
                                        {'label': 'Calls (C)', 'value': 'C'},
                                        {'label': 'Puts (P)', 'value': 'P'}
                                    ],
                                    value='C',
                                    className=INPUT_CLASSNAME,
                                    style=INPUT_STYLE
                                ),
                                width=8
                            )
                        ], className="mb-2"),

                        # MA Period
                        dbc.Row([
                            dbc.Col(dbc.Label("MA Period:", className="fw-bold"), width=4),
                            dbc.Col(
                                dcc.Input(
                                    id={'type': 'ma-period-input', 'index': index},
                                    type='number',
                                    value=5,
                                    className=INPUT_CLASSNAME,
                                    style=INPUT_STYLE
                                ),
                                width=8
                            )
                        ], className="mb-2"),

                        # Rank Days
                        dbc.Row([
                            dbc.Col(dbc.Label("Rank Days:", className="fw-bold"), width=4),
                            dbc.Col(
                                dcc.Input(
                                    id={'type': 'rank-days-input', 'index': index},
                                    type='number',
                                    value=90,
                                    className=INPUT_CLASSNAME,
                                    style=INPUT_STYLE
                                ),
                                width=8
                            )
                        ], className="mb-2"),

                        # Target DTE
                        dbc.Row([
                            dbc.Col(dbc.Label("Target DTE:", className="fw-bold"), width=4),
                            dbc.Col(
                                dcc.Input(
                                    id={'type': 'target-dte-input', 'index': index},
                                    type='number',
                                    value=25,
                                    className=INPUT_CLASSNAME,
                                    style=INPUT_STYLE
                                ),
                                width=8
                            )
                        ], className="mb-2"),

                        # Base %
                        dbc.Row([
                            dbc.Col(dbc.Label("Base %:", className="fw-bold"), width=4),
                            dbc.Col(
                                dcc.Input(
                                    id={'type': 'base-pct-input', 'index': index},
                                    type='number',
                                    value=1.0,
                                    step=0.05,
                                    className=INPUT_CLASSNAME,
                                    style=INPUT_STYLE
                                ),
                                width=8
                            )
                        ], className="mb-2"),

                        # Compare %
                        dbc.Row([
                            dbc.Col(dbc.Label("Compare %:", className="fw-bold"), width=4),
                            dbc.Col(
                                dcc.Input(
                                    id={'type': 'compare-pct-input', 'index': index},
                                    type='number',
                                    value=1.2,
                                    step=0.05,
                                    className=INPUT_CLASSNAME,
                                    style=INPUT_STYLE
                                ),
                                width=8
                            )
                        ], className="mb-3"),

                        # Buttons
                        dbc.Row([
                            dbc.Col(
                                dbc.Button(
                                    "Update Analysis",
                                    id={'type': 'update-button', 'index': index},
                                    color='primary',
                                    style={'width': '100%'}
                                ),
                                width=6
                            ),
                            dbc.Col(
                                dbc.Button(
                                    "Remove Plot",
                                    id={'type': 'remove-button', 'index': index},
                                    color='danger',
                                    style={'width': '100%'}
                                ),
                                width=6
                            ),
                        ], className="g-2")
                    ], width=3),

                    # Right Column: figure and status message
                    dbc.Col([
                        dcc.Loading(
                            id={'type': 'loading-graph', 'index': index},
                            type='default',
                            children=dcc.Graph(id={'type': 'skew-graph', 'index': index})
                        ),
                        html.Div(
                            id={'type': 'message-div', 'index': index},
                            className="mt-3 fw-bold",
                            style={'textAlign': 'left'}
                        ),
                        # This store can hold preprocessed data if needed, currently returning None
                        dcc.Store(id={'type': 'preprocessed-store', 'index': index}, storage_type='memory'),
                    ], width=9)
                ])
            ])
        ]
    )

# ------------------------------------------------------------------
# Dash App Setup and Layout
# ------------------------------------------------------------------
app = dash.Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP])

app.layout = dbc.Container([
    dbc.Row([
        dbc.Col(
            html.H1("Volatility Skew Analysis", className="text-center my-4", style={"fontSize": "1.5rem"}),
            width=12
        )
    ]),
    # Container that holds all analysis cards
    html.Div(
        id='plots-container',
        children=[create_analysis_card(0)]
    ),
    # Button to add new analysis plots
    dbc.Button(
        "Add Plot",
        id="add-plot-button",
        color="secondary",
        style={
            "position": "fixed",
            "bottom": "20px",
            "left": "20px",
            "zIndex": "999"
        }
    )
], fluid=True, style={"minHeight": "95vh"}, className="px-4")

# ------------------------------------------------------------------
# Single Callback for Adding & Removing Cards
# ------------------------------------------------------------------
@app.callback(
    Output('plots-container', 'children'),
    [
        Input('add-plot-button', 'n_clicks'),
        Input({'type': 'remove-button', 'index': ALL}, 'n_clicks')
    ],
    State('plots-container', 'children'),
    prevent_initial_call=True
)
def update_plots(add_click, remove_clicks, children):
    """
    Adds a new analysis card when 'Add Analysis Plot' is clicked,
    or removes the specified card when 'Remove Plot' is clicked.
    """
    if not ctx.triggered:
        raise dash.exceptions.PreventUpdate

    triggered_prop = ctx.triggered[0]['prop_id']
    if triggered_prop.startswith("add-plot-button"):
        new_index = len(children)
        children.append(create_analysis_card(new_index))
        return children

    if "remove-button" in triggered_prop:
        triggered_id = ctx.triggered_id
        if isinstance(triggered_id, dict):
            remove_index = triggered_id.get('index')
            new_children = []
            for c in children:
                if isinstance(c, dict) and 'props' in c and 'id' in c['props']:
                    c_id = c['props']['id']
                    if isinstance(c_id, dict):
                        # Skip the card with the matching index
                        if c_id.get('type') == 'analysis-card' and c_id.get('index') == remove_index:
                            continue
                new_children.append(c)
            return new_children

    return children

# ------------------------------------------------------------------
# Pattern Matching Callback for Each Analysis Card
# ------------------------------------------------------------------
@app.callback(
    [
        Output({'type': 'skew-graph', 'index': MATCH}, 'figure'),
        Output({'type': 'message-div', 'index': MATCH}, 'children'),
        Output({'type': 'preprocessed-store', 'index': MATCH}, 'data')
    ],
    Input({'type': 'update-button', 'index': MATCH}, 'n_clicks'),
    [
        State({'type': 'directory-input', 'index': MATCH}, 'value'),
        State({'type': 'start-date-input', 'index': MATCH}, 'value'),
        State({'type': 'ticker-input', 'index': MATCH}, 'value'),
        State({'type': 'option-side-select', 'index': MATCH}, 'value'),
        State({'type': 'ma-period-input', 'index': MATCH}, 'value'),
        State({'type': 'rank-days-input', 'index': MATCH}, 'value'),
        State({'type': 'target-dte-input', 'index': MATCH}, 'value'),
        State({'type': 'base-pct-input', 'index': MATCH}, 'value'),
        State({'type': 'compare-pct-input', 'index': MATCH}, 'value')
    ],
    prevent_initial_call=True
)
def update_analysis(n_clicks,
                    directory,
                    start_date_str,
                    ticker,
                    option_side,
                    ma_period,
                    rank_days,
                    target_dte,
                    base_pct,
                    compare_pct):
    """
    For each analysis card, loads or retrieves heavy data (based on user params),
    then computes the skew figure. Returns (figure, status message, None).
    """
    if not n_clicks:
        raise dash.exceptions.PreventUpdate

    parsed_date = None
    if start_date_str:
        try:
            parsed_date = datetime.strptime(start_date_str, '%Y-%m-%d')
        except ValueError:
            parsed_date = None

    start_time = time.time()

    # Identify heavy params for caching
    heavy_params = {
        "directory": directory,
        "start_date": start_date_str,
        "ticker": ticker,
        "option_side": option_side
    }
    key = json.dumps(heavy_params, sort_keys=True)

    # Check global cache to avoid reloading data multiple times
    global global_heavy_cache
    if key in global_heavy_cache:
        heavy_data = global_heavy_cache[key]
    else:
        heavy_data = load_and_preprocess_data(directory, parsed_date, ticker, option_side)
        if heavy_data.empty:
            errors = diagnose_missing_data(directory, start_date_str, ticker, option_side)
            if not errors:
                errors.append("No data loaded; please check your heavy parameters.")
            error_msg = " | ".join(errors)
            fig = go.Figure()
            fig.add_annotation(text=error_msg, showarrow=False, font=dict(size=20))
            return fig, error_msg, None
        global_heavy_cache[key] = heavy_data

    # Compute final figure
    fig = compute_skew_figure_from_data(
        heavy_data,
        rank_days=rank_days,
        target_dte=target_dte,
        base_pct=base_pct,
        compare_pct=compare_pct,
        option_side=option_side,
        ma_period=ma_period,
        ticker=ticker
    )

    elapsed = time.time() - start_time
    if not fig.data:
        message = "No available data after filtering."
    else:
        message = f"Processing complete. Plot updated in {elapsed:.2f} seconds."

    return fig, message, None

# ------------------------------------------------------------------
# Run the App
# ------------------------------------------------------------------
if __name__ == "__main__":
    app.run_server(host='127.0.0.1', debug=False)
