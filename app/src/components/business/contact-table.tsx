"use client";

import { useState, useMemo, useEffect } from "react";
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
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
import { Button } from "@/components/ui/button";
import { Loader2 } from "lucide-react";
import type { Person } from "@/lib/api/types";

interface ContactTableProps {
  data?: Person[];
  /** Agent RENDER markers pass contacts as `contacts` instead of `data` */
  contacts?: Record<string, unknown>[];
  /** Display title from agent marker */
  title?: string;
  search?: string;
  limit?: number;
  onSelect?: (person: Person) => void;
}

/** Normalize a person record to the expected Person shape.
 *  Handles both `name: { firstName, lastName }` (Twenty-era) and
 *  flat `firstName`/`lastName` fields (bridge responses). */
function normalizePerson(raw: Record<string, unknown>): Person {
  let firstName = "";
  let lastName = "";
  const nameField = raw.name;
  if (nameField && typeof nameField === "object" && !Array.isArray(nameField)) {
    const n = nameField as Record<string, unknown>;
    firstName = String(n.firstName ?? n.first_name ?? "");
    lastName = String(n.lastName ?? n.last_name ?? "");
  } else if (typeof nameField === "string") {
    const parts = nameField.split(/\s+/);
    firstName = parts[0] || "";
    lastName = parts.slice(1).join(" ");
  } else {
    firstName = String(raw.firstName ?? raw.first_name ?? "");
    lastName = String(raw.lastName ?? raw.last_name ?? "");
  }

  const emailField = raw.emails;
  let email = "";
  if (typeof raw.email === "string") {
    email = raw.email;
  } else if (emailField && typeof emailField === "object") {
    email = String((emailField as Record<string, unknown>).primaryEmail ?? "");
  }

  const companyField = raw.company;
  let company: { name: string } | null = null;
  if (companyField && typeof companyField === "object") {
    company = { name: String((companyField as Record<string, unknown>).name ?? "") };
  } else if (typeof companyField === "string") {
    company = { name: companyField };
  }

  const phonesField = raw.phones;
  let phone = "";
  if (typeof raw.phone === "string") {
    phone = raw.phone;
  } else if (phonesField && typeof phonesField === "object") {
    phone = String((phonesField as Record<string, unknown>).primaryPhoneNumber ?? "");
  }

  return {
    id: String(raw.id ?? ""),
    name: { firstName, lastName },
    email,
    phone,
    jobTitle: String(raw.jobTitle ?? raw.job_title ?? raw.role ?? ""),
    city: String(raw.city ?? ""),
    company,
    linkedinUrl: String(raw.linkedinUrl ?? raw.linkedin_url ?? ""),
    avatarUrl: String(raw.avatarUrl ?? raw.avatar_url ?? ""),
  };
}

const columns: ColumnDef<Person>[] = [
  {
    accessorFn: (row) =>
      `${row.name.firstName} ${row.name.lastName}`.trim(),
    id: "name",
    header: "Name",
  },
  {
    accessorKey: "email",
    header: "Email",
  },
  {
    accessorKey: "jobTitle",
    header: "Job Title",
  },
  {
    accessorFn: (row) => row.company?.name || "",
    id: "company",
    header: "Company",
  },
  {
    accessorKey: "city",
    header: "City",
  },
];

export function ContactTable({ data, contacts, title, search, limit, onSelect }: ContactTableProps) {
  const [sorting, setSorting] = useState<SortingState>([]);
  const [globalFilter, setGlobalFilter] = useState("");
  const [fetched, setFetched] = useState<Person[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Resolve provided data: `data` > `contacts` (agent marker) > self-fetch
  const hasProvidedData = (data && data.length > 0) || (contacts && contacts.length > 0);

  // Self-fetch only when no data is provided via props
  useEffect(() => {
    if (hasProvidedData) return;

    setLoading(true);
    setError(null);
    const params = new URLSearchParams();
    if (search) params.set("search", search);
    if (limit) params.set("limit", String(limit));
    const qs = params.toString();

    fetch(`/api/bridge/api/people${qs ? `?${qs}` : ""}`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((json) => {
        const raw: unknown[] = json.people || json.data || json || [];
        if (Array.isArray(raw)) {
          setFetched(raw.map((r) => normalizePerson(r as Record<string, unknown>)));
        } else {
          setFetched([]);
        }
      })
      .catch((err) => {
        setError(String(err.message || err));
        setFetched([]);
      })
      .finally(() => setLoading(false));
  }, [hasProvidedData, search, limit]);

  // Normalize: prefer data > contacts (agent) > fetched
  const normalizedData = useMemo(() => {
    if (data && data.length > 0) {
      return data.map((p) => normalizePerson(p as unknown as Record<string, unknown>));
    }
    if (contacts && contacts.length > 0) {
      return contacts.map((c) => normalizePerson(c));
    }
    if (fetched) return fetched;
    return [];
  }, [data, contacts, fetched]);

  const table = useReactTable({
    data: normalizedData,
    columns,
    state: { sorting, globalFilter },
    onSortingChange: setSorting,
    onGlobalFilterChange: setGlobalFilter,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize: 20 } },
  });

  if (loading) {
    return (
      <div className="flex items-center justify-center p-8 text-sm text-muted-foreground" data-testid="contact-table-loading">
        <Loader2 className="w-4 h-4 animate-spin mr-2" />
        Loading contacts...
      </div>
    );
  }

  if (error && normalizedData.length === 0) {
    return (
      <div className="p-4 text-sm text-destructive" data-testid="contact-table-error">
        Failed to load contacts: {error}
      </div>
    );
  }

  return (
    <div className="space-y-4" data-testid="contact-table">
      {title && (
        <h3 className="text-sm font-semibold text-foreground">{title}</h3>
      )}
      <Input
        placeholder="Search contacts..."
        value={globalFilter}
        onChange={(e) => setGlobalFilter(e.target.value)}
        data-testid="contact-search"
      />
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
                    data-testid={`header-${header.id}`}
                  >
                    {flexRender(
                      header.column.columnDef.header,
                      header.getContext()
                    )}
                    {{ asc: " ↑", desc: " ↓" }[
                      header.column.getIsSorted() as string
                    ] ?? ""}
                  </TableHead>
                ))}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {table.getRowModel().rows.map((row) => (
              <TableRow
                key={row.id}
                className="cursor-pointer hover:bg-accent/50"
                onClick={() => onSelect?.(row.original)}
                data-testid="contact-row"
              >
                {row.getVisibleCells().map((cell) => (
                  <TableCell key={cell.id}>
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      {table.getPageCount() > 1 && (
        <div className="flex items-center justify-between">
          <p className="text-sm text-muted-foreground">
            Page {table.getState().pagination.pageIndex + 1} of{" "}
            {table.getPageCount()}
          </p>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => table.previousPage()}
              disabled={!table.getCanPreviousPage()}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => table.nextPage()}
              disabled={!table.getCanNextPage()}
            >
              Next
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
