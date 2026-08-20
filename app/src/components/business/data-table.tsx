"use client";

import { useMemo, useState } from "react";
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  getFilteredRowModel,
  flexRender,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Row = Record<string, any>;

interface DataTableProps {
  title?: string;
  /** Primary data prop */
  data?: Row[];
  /** LLM-friendly alias for data */
  rows?: Row[];
  /** ColumnDef[] or simple string[] (auto-converted) */
  columns?: ColumnDef<Row>[] | string[];
}

/**
 * Convert LLM-friendly props into TanStack Table format.
 * Accepts columns as string[] (["name","email"]) or ColumnDef[].
 * Accepts data as `data` or `rows`. Auto-infers columns from row keys if omitted.
 */
function normalizeColumns(
  columns: DataTableProps["columns"],
  rows: Row[],
): ColumnDef<Row>[] {
  // If proper ColumnDef objects, use as-is
  if (columns && columns.length > 0 && typeof columns[0] === "object") {
    return columns as ColumnDef<Row>[];
  }

  // String array → auto-generate ColumnDef
  const keys: string[] =
    Array.isArray(columns) && columns.length > 0
      ? (columns as string[])
      : rows.length > 0
        ? Object.keys(rows[0])
        : [];

  return keys.map((key) => ({
    accessorKey: key,
    header: key.charAt(0).toUpperCase() + key.slice(1).replace(/_/g, " "),
  }));
}

export function DataTable({ title, data, rows, columns }: DataTableProps) {
  const resolvedData = useMemo(() => data ?? rows ?? [], [data, rows]);
  const resolvedColumns = useMemo(
    () => normalizeColumns(columns, resolvedData),
    [columns, resolvedData],
  );

  const [sorting, setSorting] = useState<SortingState>([]);
  const [globalFilter, setGlobalFilter] = useState("");

  // eslint-disable-next-line react-hooks/incompatible-library -- useReactTable returns non-memoizable functions; React Compiler skips this component by design
  const table = useReactTable({
    data: resolvedData,
    columns: resolvedColumns,
    state: { sorting, globalFilter },
    onSortingChange: setSorting,
    onGlobalFilterChange: setGlobalFilter,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  });

  return (
    <Card className="glass-panel" data-testid="data-table">
      <CardHeader className="pb-2">
        {title && <CardTitle className="text-sm font-medium">{title}</CardTitle>}
        <Input
          placeholder="Filter..."
          value={globalFilter}
          onChange={(e) => setGlobalFilter(e.target.value)}
          className="max-w-sm"
        />
      </CardHeader>
      <CardContent>
        <div className="rounded-md border border-border">
          <Table>
            <TableHeader>
              {table.getHeaderGroups().map((headerGroup) => (
                <TableRow key={headerGroup.id}>
                  {headerGroup.headers.map((header) => (
                    <TableHead
                      key={header.id}
                      className="cursor-pointer select-none"
                      onClick={header.column.getToggleSortingHandler()}
                    >
                      {flexRender(
                        header.column.columnDef.header,
                        header.getContext()
                      )}
                    </TableHead>
                  ))}
                </TableRow>
              ))}
            </TableHeader>
            <TableBody>
              {table.getRowModel().rows.map((row) => (
                <TableRow key={row.id}>
                  {row.getVisibleCells().map((cell) => (
                    <TableCell key={cell.id}>
                      {flexRender(
                        cell.column.columnDef.cell,
                        cell.getContext()
                      )}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}
