import calendar
import json
import os
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from google.cloud.bigquery import Client
from google.cloud.bigquery.job import QueryJob, QueryJobConfig
from google.cloud.bigquery.table import RowIterator
from packaging.utils import canonicalize_name

from pypinfo.fields import AGGREGATES, Downloads, Field

FROM = 'FROM `bigquery-public-data.pypi.file_downloads`'
DATE_ADD = 'TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL {} DAY)'
START_TIMESTAMP = 'TIMESTAMP("{} 00:00:00")'
END_TIMESTAMP = 'TIMESTAMP("{} 23:59:59")'
START_DATE = '-31'
END_DATE = '-1'
DEFAULT_LIMIT = 10

Rows = List[List[str]]


def create_config() -> QueryJobConfig:
    config = QueryJobConfig()
    config.use_legacy_sql = False
    return config


def normalize_dates(start_date: str, end_date: str) -> Tuple[str, str]:
    """If a date is yyyy-mm, normalize as first or last yyyy-mm-dd of the month.
    Otherwise, return unchanged.
    """
    try:
        start_date, _ = month_ends(start_date)  # yyyy-mm
    except (AttributeError, ValueError):
        pass  # -n, yyyy-mm-dd

    try:
        _, end_date = month_ends(end_date)  # yyyy-mm
    except (AttributeError, ValueError):
        pass  # -n, yyyy-mm-dd

    return start_date, end_date


def create_client(creds_file: Optional[str] = None) -> Client:
    creds_file = creds_file or os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')

    if creds_file is None:
        raise SystemError('Credentials could not be found.')

    project = json.load(open(creds_file))['project_id']
    return Client.from_service_account_json(creds_file, project=project)


def validate_date(date_text: str) -> bool:
    """Return True if valid, raise ValueError if not"""
    try:
        if int(date_text) < 0:
            return True
    except ValueError:
        pass

    try:
        datetime.strptime(date_text, '%Y-%m-%d')
        return True
    except ValueError:
        pass

    raise ValueError('Dates must be negative integers or YYYY-MM[-DD] in the past.')


def format_date(date: str, timestamp_format: str) -> str:
    try:
        date = DATE_ADD.format(int(date))
    except ValueError:
        date = timestamp_format.format(date)
    return date


def month_ends(yyyy_mm: str) -> Tuple[str, str]:
    """Helper to return start_date and end_date of a month as yyyy-mm-dd"""
    year, month = map(int, yyyy_mm.split("-"))
    first = date(year, month, 1)
    number_of_days = calendar.monthrange(year, month)[1]
    last = date(year, month, number_of_days)
    return str(first), str(last)


def build_query(
    project: str,
    all_fields: Iterable[Field],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    days: Optional[int] = None,
    limit: Optional[int] = None,
    where: Optional[str] = None,
    order: Optional[str] = None,
    pip: bool = False,
) -> str:
    project = canonicalize_name(project)

    start_date = start_date or START_DATE
    end_date = end_date or END_DATE
    limit = limit or DEFAULT_LIMIT

    if days:
        start_date = str(int(end_date) - days)

    start_date, end_date = normalize_dates(start_date, end_date)
    validate_date(start_date)
    validate_date(end_date)

    try:
        if int(start_date) >= int(end_date):
            raise ValueError('End date must be greater than start date.')
    except ValueError:
        # Not integers, must be yyyy-mm-dd
        pass

    start_date = format_date(start_date, START_TIMESTAMP)
    end_date = format_date(end_date, END_TIMESTAMP)

    fields: List[Field] = []
    used_fields = set()
    for f in all_fields:
        if f not in used_fields:
            fields.append(f)
            used_fields.add(f)

    fields.append(Downloads)
    query = 'SELECT\n'

    for field in fields:
        query += f'  {field.data} as {field.name},\n'

    query += FROM

    query += f'\nWHERE timestamp BETWEEN {start_date} AND {end_date}\n'
    if where:
        query += f'  AND {where}\n'
    else:
        conditions = []
        if project:
            conditions.append(f'file.project = "{project}"\n')
        if pip:
            conditions.append('details.installer.name = "pip"\n')
        if conditions:
            query += '  AND '
            query += '  AND '.join(conditions)

    if len(fields) > 1:
        gb = 'GROUP BY\n'
        initial_length = len(gb)

        non_aggregate_fields = []
        for field in fields[:-1]:
            if field not in AGGREGATES:
                non_aggregate_fields.append(field.name)
        gb += '  '
        gb += ', '.join(non_aggregate_fields)
        gb += '\n'

        if len(gb) > initial_length:
            query += gb

    query += 'ORDER BY\n  {} DESC\n'.format(order or Downloads.name)
    query += f'LIMIT {limit}'

    return query


def parse_query_result(query_job: QueryJob, query_rows: RowIterator) -> Rows:
    rows = [[field.name for field in query_job.result().schema]]
    rows.extend([str(item) for item in row] for row in query_rows)
    return rows


def add_percentages(rows: Rows, include_sign: bool = True) -> Rows:

    headers = rows.pop(0)
    index = headers.index('download_count')
    headers.insert(index, 'percent')
    total_downloads = sum(int(row[index]) for row in rows)
    percent_format = '{:.2%}' if include_sign else '{:.2}'

    for row in rows:
        percent = percent_format.format(int(row[index]) / total_downloads)
        row.insert(index, percent)

    rows.insert(0, headers)
    return rows


def get_download_total(rows: Rows) -> Tuple[int, int]:
    """Return the total downloads, and the downloads column"""
    headers = rows.pop(0)
    index = headers.index('download_count')
    total_downloads = sum(int(row[index]) for row in rows)

    rows.insert(0, headers)
    return total_downloads, index


def add_download_total(rows: Rows) -> Rows:
    """Add a final row to rows showing the total downloads"""
    total_row = [""] * len(rows[0])
    total_row[0] = "Total"
    total_downloads, downloads_column = get_download_total(rows)
    total_row[downloads_column] = str(total_downloads)
    rows.append(total_row)

    return rows


def tabulate(rows: Rows, markdown: bool = False) -> str:
    column_widths = [0] * len(rows[0])
    right_align = [[False] * len(rows[0])] * len(rows)

    # Get max width of each column
    for r, row in enumerate(rows):
        for i, item in enumerate(row):
            if item.isdigit():
                # Separate the thousands
                rows[r][i] = "{:,}".format(int(item))
                right_align[r][i] = True
            elif item.endswith('%'):
                right_align[r][i] = True
            length = len(item)
            if length > column_widths[i]:
                column_widths[i] = length

    tabulated = '| '

    headers = rows.pop(0)
    for i, item in enumerate(headers):
        tabulated += item + ' ' * (column_widths[i] - len(item) + 1) + '| '

    tabulated = tabulated.rstrip()
    tabulated += '\n| '

    for i in range(len(rows[0])):
        tabulated += '-' * (column_widths[i] - 1)
        if right_align[0][i] and markdown:
            tabulated += ': | '
        else:
            tabulated += '- | '

    tabulated = tabulated.rstrip()
    tabulated += '\n'

    for r, row in enumerate(rows):
        for i, item in enumerate(row):
            num_spaces = column_widths[i] - len(item)
            tabulated += '| '
            if right_align[r][i]:
                tabulated += ' ' * num_spaces + item + ' '
            else:
                tabulated += item + ' ' * (num_spaces + 1)
        tabulated += '|\n'

    return tabulated


def format_json(rows: Rows, query_info: Dict[str, Any], indent: Optional[int]) -> str:
    headers, *_data = rows
    data: List[Any] = _data
    items: List[Dict[str, Any]] = []

    for d in data:
        item: Dict[str, Any] = {}
        for i in range(len(headers)):
            if d[i].isdigit():
                d[i] = int(d[i])
            item[headers[i]] = d[i]
        items.append(item)

    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

    j = {'last_update': now, 'rows': items, 'query': query_info}

    separators = (',', ':') if indent is None else None
    return json.dumps(j, indent=indent, separators=separators, sort_keys=True)
