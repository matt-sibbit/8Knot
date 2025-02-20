from dash import html, dcc, callback
import dash
import dash_bootstrap_components as dbc
from dash.dependencies import Input, Output, State
import plotly.graph_objects as go
import pandas as pd
import logging
from dateutil.relativedelta import *  # type: ignore
import plotly.express as px
from pages.utils.graph_utils import get_graph_time_values, color_seq
from queries.pr_response_query import pr_response_query as prr
import io
from cache_manager.cache_manager import CacheManager as cm
from pages.utils.job_utils import nodata_graph
import time

PAGE = "contributions"
VIZ_ID = "pr-first-response"

gc_pr_first_response = dbc.Card(
    [
        dbc.CardBody(
            [
                html.H3(
                    "Pull Request Time to First Response",
                    className="card-title",
                    style={"textAlign": "center"},
                ),
                dbc.Popover(
                    [
                        dbc.PopoverHeader("Graph Info:"),
                        dbc.PopoverBody(
                            """
                            Compares the volume of PRs being opened against the number of those PRs that \n
                            receive at least one response within the parameterized timeframe after being opened.
                            """
                        ),
                    ],
                    id=f"popover-{PAGE}-{VIZ_ID}",
                    target=f"popover-target-{PAGE}-{VIZ_ID}",
                    placement="top",
                    is_open=False,
                ),
                dcc.Loading(
                    dcc.Graph(id=f"{PAGE}-{VIZ_ID}"),
                ),
                dbc.Form(
                    [
                        dbc.Row(
                            [
                                dbc.Label(
                                    "Response Days:",
                                    html_for=f"response-days-{PAGE}-{VIZ_ID}",
                                    width="auto",
                                ),
                                dbc.Col(
                                    dbc.Input(
                                        id=f"response-days-{PAGE}-{VIZ_ID}",
                                        type="number",
                                        min=1,
                                        max=120,
                                        step=1,
                                        value=2,
                                    ),
                                    className="me-2",
                                    width=1,
                                ),
                                dbc.Col(
                                    width=6,
                                ),
                                dbc.Col(
                                    dbc.Button(
                                        "About Graph",
                                        id=f"popover-target-{PAGE}-{VIZ_ID}",
                                        color="secondary",
                                        size="sm",
                                    ),
                                    width="auto",
                                    style={"paddingTop": ".5em"},
                                ),
                            ],
                            align="center",
                            justify="between",
                        ),
                    ]
                ),
            ]
        )
    ],
)


# callback for graph info popover
@callback(
    Output(f"popover-{PAGE}-{VIZ_ID}", "is_open"),
    [Input(f"popover-target-{PAGE}-{VIZ_ID}", "n_clicks")],
    [State(f"popover-{PAGE}-{VIZ_ID}", "is_open")],
)
def toggle_popover(n, is_open):
    if n:
        return not is_open
    return is_open


# callback for pr first response graph
@callback(
    Output(f"{PAGE}-{VIZ_ID}", "figure"),
    [
        Input("repo-choices", "data"),
        Input(f"response-days-{PAGE}-{VIZ_ID}", "value"),
    ],
    background=True,
)
def pr_first_response_graph(repolist, num_days):
    # wait for data to asynchronously download and become available.
    cache = cm()
    df = cache.grabm(func=prr, repos=repolist)
    while df is None:
        time.sleep(1.0)
        df = cache.grabm(func=prr, repos=repolist)

    start = time.perf_counter()
    logging.warning(f"{VIZ_ID}- START")

    # test if there is data
    if df.empty:
        logging.warning(f"{VIZ_ID} - NO DATA AVAILABLE")
        return nodata_graph

    df = process_data(df, num_days)

    fig = create_figure(df, num_days)

    logging.warning(f"{VIZ_ID} - END - {time.perf_counter() - start}")
    return fig


def process_data(df: pd.DataFrame, num_days):

    # convert to datetime objects rather than strings
    df["msg_timestamp"] = pd.to_datetime(df["msg_timestamp"], utc=True)
    df["pr_created_at"] = pd.to_datetime(df["pr_created_at"], utc=True)
    df["pr_closed_at"] = pd.to_datetime(df["pr_closed_at"], utc=True)

    # drop messages from the pr creator
    df = df[df["cntrb_id"] != df["msg_cntrb_id"]]

    # sort in ascending earlier and only get ealiest value
    df = df.sort_values(by="msg_timestamp", axis=0, ascending=True)
    df = df.drop_duplicates(subset="pull_request_id", keep="first")

    # first and last elements of the dataframe are the
    # earliest and latest events respectively
    earliest = df["pr_created_at"].min()
    latest = max(df["pr_created_at"].max(), df["pr_closed_at"].max())

    # beginning to the end of time by the specified interval
    dates = pd.date_range(start=earliest, end=latest, freq="D", inclusive="both")

    # df for open prs and responded to prs in time interval
    df_pr_responses = dates.to_frame(index=False, name="Date")

    # every day, count the number of PRs that are open on that day and the number of
    # those that were responded to within num_days of their opening
    df_pr_responses["Open"], df_pr_responses["Response"] = zip(
        *df_pr_responses.apply(
            lambda row: get_open_response(df, row.Date, num_days),
            axis=1,
        )
    )

    df_pr_responses["Date"] = df_pr_responses["Date"].dt.strftime("%Y-%m-%d")

    return df_pr_responses


def create_figure(df: pd.DataFrame, num_days):

    fig = go.Figure(
        [
            go.Scatter(
                name="Prs Open",
                x=df["Date"],
                y=df["Open"],
                mode="lines",
                showlegend=True,
                hovertemplate="PR's Open: %{y}<br>%{x|%b %d, %Y} <extra></extra>",
                marker=dict(color=color_seq[1]),
            ),
            go.Scatter(
                name="Response <" + str(num_days) + " days",
                x=df["Date"],
                y=df["Response"],
                mode="lines",
                showlegend=True,
                hovertemplate="PRs: %{y}<br>%{x|%b %d, %Y} <extra></extra>",
                marker=dict(color=color_seq[5]),
            ),
        ]
    )
    fig.update_layout(
        xaxis_title="Time",
        yaxis_title="Number of PRs",
        font=dict(size=14),
    )

    return fig


def get_open_response(df, date, num_days):
    """
    This function takes a date and determines how many
    prs in that time interval are opened and if they have a response within num_days.

    Args:
    -----
        df : Pandas Dataframe
            Dataframe with pr assignment actions of the assignees

        date : Datetime Timestamp
            Timestamp of the date

        num_days : int
            number of days that a response should be within

    Returns:
    --------
        int, int: Number of opened and responded to prs within num_days on the day
    """
    # drop rows that are more recent than the date limit
    df_created = df[df["pr_created_at"] <= date]

    # drops rows that have been closed after date
    df_open = df_created[df_created["pr_closed_at"] > date]

    # include prs that have not been close yet
    df_open = pd.concat([df_open, df_created[df_created.pr_closed_at.isnull()]])

    # column to hold date num_days after the pr_creation date for comparision
    df_open["response_by"] = df_open["pr_created_at"] + pd.DateOffset(days=num_days)

    # Inlcude only the prs that msg timestamp is before the responded by time
    df_response = df_open[df_open["msg_timestamp"] < df_open["response_by"]]

    # generates number of columns ie open prs
    num_open = df_open.shape[0]

    # number of prs that had response in time interval
    num_response = df_response.shape[0]

    return num_open, num_response
