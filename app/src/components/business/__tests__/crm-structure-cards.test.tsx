import { render, screen, within } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { ContactCard } from "../contact-card";
import { CompanyCard } from "../company-card";
import type { Affiliation, Company, Person } from "@/lib/api/types";

const person: Person = {
  id: "p1",
  name: { firstName: "Alice", lastName: "Example" },
  email: "alice@example.com",
};

const company: Company = { id: "c1", name: "Child Office" };

function affiliation(over: Partial<Affiliation>): Affiliation {
  return {
    id: "a1",
    personId: "p1",
    personName: "Alice Example",
    companyId: "c1",
    companyName: "Child Office",
    role: "owner",
    isActive: true,
    since: "2026-01-05T00:00:00+00:00",
    until: null,
    ...over,
  };
}

describe("ContactCard affiliations", () => {
  it("shows each company with the role and dates", () => {
    render(
      <ContactCard
        person={person}
        affiliations={[
          affiliation({}),
          affiliation({
            id: "a2",
            companyName: "Parent Group",
            role: "manager",
            isActive: false,
            until: "2026-06-30T00:00:00+00:00",
          }),
        ]}
      />
    );
    const rows = screen.getAllByTestId("affiliation-row");
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain("Child Office");
    expect(rows[0].textContent).toContain("owner");
    expect(rows[0].textContent).toContain("2026-01-05");
    expect(rows[0].textContent).not.toMatch(/inactive/i);
    expect(rows[1].textContent).toContain("Parent Group");
    expect(rows[1].textContent).toMatch(/inactive/i);
    expect(rows[1].textContent).toContain("2026-06-30");
  });

  it("renders exactly as before when there are no affiliations", () => {
    render(<ContactCard person={person} />);
    expect(screen.getByTestId("contact-card").textContent).toContain("Alice Example");
    expect(screen.queryByTestId("contact-affiliations")).toBeNull();
  });
});

describe("CompanyCard structure", () => {
  it("shows the parent, the children and the people by role", () => {
    render(
      <CompanyCard
        company={company}
        parent={{ id: "c0", name: "Parent Group" }}
        subsidiaries={[
          { id: "c2", name: "Satellite East" },
          { id: "c3", name: "Satellite West" },
        ]}
        affiliations={[
          affiliation({ personName: "Alice Example", role: "owner" }),
          affiliation({ id: "a2", personId: "p2", personName: "Bob Example", role: "provider" }),
          affiliation({ id: "a3", personId: "p3", personName: "Carol Example", role: "provider" }),
        ]}
      />
    );
    expect(screen.getByTestId("company-parent").textContent).toContain("Parent Group");
    const children = screen.getByTestId("company-children");
    expect(within(children).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Satellite East",
      "Satellite West",
    ]);
    const groups = screen.getAllByTestId("company-role-group");
    expect(groups).toHaveLength(2);
    const provider = groups.find((g) => g.textContent?.includes("provider"));
    expect(provider?.textContent).toContain("Bob Example");
    expect(provider?.textContent).toContain("Carol Example");
  });

  it("renders exactly as before for a flat company", () => {
    render(<CompanyCard company={{ ...company, domainName: "example.com" }} />);
    expect(screen.getByTestId("company-card").textContent).toContain("example.com");
    expect(screen.queryByTestId("company-parent")).toBeNull();
    expect(screen.queryByTestId("company-children")).toBeNull();
    expect(screen.queryByTestId("company-people")).toBeNull();
  });
});
