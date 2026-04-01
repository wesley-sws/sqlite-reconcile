#!/usr/bin/env python3
import sys
import sqlite3
import argparse
import subprocess
import utils
import shutil
import tempfile
import os
import sqlglot
from sqlglot import (Expression, expressions)
from collections import defaultdict
from enum import Enum
from dataclasses import dataclass
import json

@dataclass
class DiffBuckets:
    matched_ours_indices: list[int]
    extra_ours_indices: list[int]
    extra_theirs_indices: list[int]
    conflict_pairs: list[tuple[int, int]]

transaction_begin = "BEGIN TRANSACTION;"
transaction_end = "COMMIT;"

def apply_sql_to_temp_db(source_db_path, sql_script):
    fd, temp_db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd) # close low-level file descriptor
    try:
        shutil.copy2(source_db_path, temp_db_path)
        proc = subprocess.run(
            ["sqlite3", temp_db_path],
            input=sql_script,      # SQL string in memory
            text=True,
            capture_output=True
        )
        print(proc.stdout)
        if proc.returncode != 0:
            raise RuntimeError(f"sqlite3 failed: {proc.stderr.strip()}")

        return temp_db_path  # caller can keep using this merged temp DB
    except:
        if os.path.exists(temp_db_path):
            os.remove(temp_db_path)
        raise

def table_primary_keys(table_name: str, db_dir: str) -> dict[str, int]:
    with sqlite3.connect(db_dir) as con:
        cursor = con.cursor()
        res = cursor.execute(f"SELECT pk, name FROM pragma_table_info('{table_name}') WHERE pk <> 0")
        key_column_to_index: dict[str, int] = {name: index-1 for (index, name) in res.fetchall()}
        return key_column_to_index

def get_primary_key_values_from_where(key_column_to_index: dict[str, int], expr: Expression) -> None | tuple[expressions.Literal, ...]:
    primary_values = [None for _ in range(len(key_column_to_index))]
    where = expr.find(expressions.Where)
    if where is None:
        return None
    for eq in where.find_all(expressions.EQ):
        assert isinstance(eq.this, expressions.Column) and isinstance(eq.expression, expressions.Literal)
        assert eq.this.name in key_column_to_index
        primary_values[key_column_to_index[eq.this.name]] = eq.expression # type: ignore
    assert all(v is not None for v in primary_values)
    return tuple(primary_values) # type: ignore

def get_key_values_and_column_to_literal(
        row: Expression, schema: Expression, key_column_to_index: dict[str, int]
        ) -> tuple[tuple[Expression, ...], dict[str, Expression]]:
    column_to_literal = {}
    primary_values = [None for _ in range(len(key_column_to_index))]
    for ident, lit in zip(schema.expressions, row.expressions):
        if ident.name in key_column_to_index:
            primary_values[key_column_to_index[ident.name]] = lit
        else:
            column_to_literal[ident.name] = lit
    assert all(v is not None for v in primary_values)
    return tuple(primary_values), column_to_literal # type: ignore

def check_conflict_and_return_final_diff(
        base_to_ours_parsed: list[Expression], 
        base_to_theirs_parsed: list[Expression],
        base_dir: str) -> DiffBuckets:
    '''approach:
    - build hash table keyed by primary key from base_to_ours_parsed
    - iterate through base_to_theirs_parsed for conflict / safe operations, raise
    exception and/or update the parsed list where applicable
    - return updated parsed list to apply / conflict if any
    Changes to data can be classified into insert, update and delete statements
    Let's consider the combinations of the statements with the same row (identified
    by primary key) in both base_to_ours and base_to_theirs 
    - insert-insert: check values the same
    - insert-update or insert-delete don't make sense so no need to consider
    - delete-delete: cross appliaction and diffing would handle this (no conflict)
    - update-update: check values updated the same
    - update-delete: check primary key the same, and if YES raise conflict
    with sqldiff, the update and delete will always have its primary key value
    in the where clause
    '''
    table_name_to_key_value_to_data: dict[
        str, 
        dict[tuple[expressions.Literal, ...], 
             tuple[int, type, dict[str, Expression] | None]]
        ] = defaultdict(dict)
    # build hashed table
    table_name_to_key_column_to_index = defaultdict(dict)
    for i, expr in enumerate(base_to_ours_parsed):
        table = expr.find(expressions.Table)
        assert table is not None
        key_values_to_data = table_name_to_key_value_to_data[table.name]
        if table.name not in table_name_to_key_column_to_index:
            table_name_to_key_column_to_index[table.name] = table_primary_keys(table.name, base_dir)
        key_column_to_index = table_name_to_key_column_to_index[table.name]
        key_values = get_primary_key_values_from_where(
            key_column_to_index,
            expr
        )
        if isinstance(expr, expressions.Delete):
            assert key_values is not None
            key_values_to_data[key_values] = (i, expressions.Delete, None)
        elif isinstance(expr, expressions.Insert):
            for row in expr.expression.expressions:
                primary_values_tuple, column_to_literal = \
                    get_key_values_and_column_to_literal(row, expr.this, key_column_to_index)
                key_values_to_data[tuple(primary_values_tuple)] = (i, expressions.Insert, column_to_literal) # type: ignore
        elif isinstance(expr, expressions.Update):
            column_to_literal = {}
            assert key_values is not None
            for e in expr.args.get("expressions") or []:
                column_to_literal[e.this.name] = e.expression
            key_values_to_data[key_values] = (i, expressions.Update, column_to_literal)
    res_match = [] # using index from base_to_ours
    extra_stmts_base_to_ours = []
    extra_stmts_base_to_theirs = []
    conflict_stmts_pair = [] # (base_to_ours index, base_to_theirs index)
    for i, expr in enumerate(base_to_theirs_parsed):
        table = expr.find(expressions.Table)
        assert table is not None
        if table.name not in table_name_to_key_column_to_index:
            # no stmts from base_to_ours on this table
            extra_stmts_base_to_theirs.append(i)
            continue
        key_column_to_index = table_name_to_key_column_to_index[table.name]
        key_values_to_data = table_name_to_key_value_to_data[table.name]
        key_values = get_primary_key_values_from_where(
            key_column_to_index,
            expr
        )
        if isinstance(expr, expressions.Delete):
            assert key_values is not None
            if key_values not in key_values_to_data:
                extra_stmts_base_to_theirs.append(i)
            else:
                i2, t, column_to_literal = key_values_to_data[key_values]
                if t is expressions.Delete:
                    res_match.append(i2)
                elif t is expressions.Update:
                    conflict_stmts_pair.append((i2, i))
                key_values_to_data.pop(key_values)

        elif isinstance(expr, expressions.Insert):
            for row in expr.expression.expressions:
                primary_values_tuple, curr_column_to_literal = \
                    get_key_values_and_column_to_literal(row, expr.this, key_column_to_index)
                if primary_values_tuple not in key_values_to_data:
                    extra_stmts_base_to_theirs.append(i)
                else:
                    i2, t, column_to_literal = key_values_to_data[primary_values_tuple]
                    assert t is expressions.Insert
                    if column_to_literal == curr_column_to_literal:
                        res_match.append(i2)
                    else:
                        conflict_stmts_pair.append((i2, i))
                    key_values_to_data.pop(primary_values_tuple)
        elif isinstance(expr, expressions.Update):
            assert key_values is not None
            if key_values not in key_values_to_data:
                extra_stmts_base_to_theirs.append(i)
            else:
                i2, t, column_to_literal = key_values_to_data[key_values]
                if t is expressions.Delete:
                    res_match.append(i2)
                elif t is expressions.Update:
                    curr_column_to_literal = {}
                    assert key_values is not None
                    for e in expr.args.get("expressions") or []:
                        curr_column_to_literal[e.this.name] = e.expression
                    if curr_column_to_literal == column_to_literal:
                        res_match.append(i2)
                    else:
                        conflict_stmts_pair.append((i2, i))
                key_values_to_data.pop(key_values)
    for key_value_to_data in table_name_to_key_value_to_data.values():
        extra_stmts_base_to_ours.extend(data[0] for data in key_value_to_data.values())
    return DiffBuckets(res_match, extra_stmts_base_to_ours, extra_stmts_base_to_theirs, conflict_stmts_pair)

def main():
    parser = argparse.ArgumentParser(description='SQLite merge driver')
    parser.add_argument('base', metavar='%O', help='Base file')
    parser.add_argument('ours', metavar='%A', help='Ours file')
    parser.add_argument('theirs', metavar='%B', help='Theirs file')
    # irrelevant for our case
    parser.add_argument('conflict_marker_size', metavar='%L', type=int, help='Conflict marker size')
    # %P - path of file as it appears in repo - potentially useful for debugging or logging, ignore for now
    parser.add_argument('pathname', metavar='%P', help='Pathname of file being merged')
    args = parser.parse_args()
    # res1/2: start and end of list are begin and commit of transaction, the rest being the sql statements
    base_to_ours = utils.subprocess_run_wrapper(
        ["sqldiff", "--primarykey", args.base, args.ours], text=True, capture_output=True
        ).stdout.strip().split('\n')
    base_to_theirs = utils.subprocess_run_wrapper(
        ["sqldiff", "--primarykey", args.base, args.theirs], text=True, capture_output=True
        ).stdout.strip().split('\n')
    
    base_to_ours_parsed: list[Expression] = [sqlglot.parse_one(statement) for statement in base_to_ours]
    base_to_theirs_parsed: list[Expression] = [sqlglot.parse_one(statement) for statement in base_to_theirs]
    diffs = check_conflict_and_return_final_diff(base_to_ours_parsed, base_to_theirs_parsed, args.base)
    stmts_to_apply = \
        [base_to_ours[i] for i in diffs.matched_ours_indices] + \
        [base_to_ours[i] for i in diffs.extra_ours_indices] + \
        [base_to_theirs[i] for i in diffs.extra_theirs_indices]
    shutil.copy2(args.base, args.ours)
    utils.subprocess_run_wrapper(
        ['sqlite3', args.ours], 
        input='\n'.join(stmts_to_apply), 
        text=True, 
        capture_output=True)
    if len(diffs.conflict_pairs) > 0:
        out = {
            "conflict pairs": [[base_to_ours[i], base_to_theirs[j]] for i, j in diffs.conflict_pairs]
        }
        conflict_file = f"{args.pathname}-.merge_file.json"
        with open(conflict_file, "w") as f:
            json.dump(out, f, indent=2)
        sys.exit(1)
    sys.exit(0)

if __name__ == '__main__':
    main()