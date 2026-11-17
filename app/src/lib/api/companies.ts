import { apiFetch } from "./client";
import type { Affiliation, Company, CompanyStructure } from "./types";

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

/** Parent, parent chain, children, people by role and outside ids of a company. */
export async function fetchCompanyStructure(id: string): Promise<CompanyStructure> {
  return apiFetch<CompanyStructure>(`/api/bridge/api/companies/${id}/structure`);
}

export async function fetchCompanyAffiliations(
  id: string,
  role?: string
): Promise<Affiliation[]> {
  const params = role ? `?role=${encodeURIComponent(role)}` : "";
  const res = await apiFetch<{ affiliations: Affiliation[] }>(
    `/api/bridge/api/companies/${id}/affiliations${params}`
  );
  return res.affiliations;
}
