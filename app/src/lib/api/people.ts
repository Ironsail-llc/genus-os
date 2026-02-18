import { apiFetch } from "./client";
import type { Person } from "./types";

interface RawPerson {
  id: string;
  name: { firstName: string; lastName: string };
  emails?: { primaryEmail?: string };
  phones?: { primaryPhoneNumber?: string };
  email?: string;
  phone?: string;
  jobTitle?: string;
  city?: string;
  company?: { name: string; id?: string } | null;
  linkedinUrl?: string;
  avatarUrl?: string;
  createdAt?: string;
  updatedAt?: string;
}

function normalizePerson(raw: RawPerson): Person {
  return {
    ...raw,
    email: raw.email || raw.emails?.primaryEmail || undefined,
    phone: raw.phone || raw.phones?.primaryPhoneNumber || undefined,
  };
}

export async function fetchPeople(search?: string): Promise<Person[]> {
  const params = search ? `?search=${encodeURIComponent(search)}` : "";
  const res = await apiFetch<{ people: RawPerson[] }>(
    `/api/bridge/api/people${params}`
  );
  return (res.people ?? []).map(normalizePerson);
}

export async function fetchPerson(id: string): Promise<Person> {
  return apiFetch<Person>(`/api/bridge/api/people/${id}`);
}

export async function createPerson(
  data: Partial<Person> & { name: { firstName: string; lastName: string } }
): Promise<Person> {
  return apiFetch<Person>("/api/bridge/api/people", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function searchPeople(query: string): Promise<Person[]> {
  return fetchPeople(query);
}
