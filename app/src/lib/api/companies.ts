import { apiFetch } from "./client";
import type { Company } from "./types";

export async function fetchCompanies(search?: string): Promise<Company[]> {
  const params = search ? `?search=${encodeURIComponent(search)}` : "";
  return apiFetch<Company[]>(`/api/bridge/api/companies${params}`);
}

export async function fetchCompany(id: string): Promise<Company> {
  return apiFetch<Company>(`/api/bridge/api/companies/${id}`);
}

export async function createCompany(
  data: Partial<Company> & { name: string }
): Promise<Company> {
  return apiFetch<Company>("/api/bridge/api/companies", {
    method: "POST",
    body: JSON.stringify(data),
  });
}
