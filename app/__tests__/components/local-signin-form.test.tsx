import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { signIn } from "next-auth/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LocalSignInForm } from "@/components/local-signin-form";

vi.mock("next-auth/react", () => ({ signIn: vi.fn() }));

const mockSignIn = vi.mocked(signIn);

afterEach(() => {
  vi.resetAllMocks();
});

function fill(email = "alice@example.com", password = "correct horse battery") {
  fireEvent.change(screen.getByLabelText(/email/i), { target: { value: email } });
  fireEvent.change(screen.getByLabelText(/^password$/i), { target: { value: password } });
}

describe("LocalSignInForm", () => {
  it("renders email and password, and no code field until one is asked for", () => {
    render(<LocalSignInForm callbackUrl="/" />);
    expect(screen.getByLabelText(/email/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^password$/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/one-time code/i)).not.toBeInTheDocument();
  });

  it("signs in through the local provider without a page redirect", async () => {
    mockSignIn.mockResolvedValue({ ok: true, error: undefined } as never);
    render(<LocalSignInForm callbackUrl="/dashboard" />);
    fill();
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => expect(mockSignIn).toHaveBeenCalledTimes(1));
    expect(mockSignIn.mock.calls[0][0]).toBe("local");
    expect(mockSignIn.mock.calls[0][1]).toMatchObject({
      email: "alice@example.com",
      password: "correct horse battery",
      redirect: false,
    });
  });

  it("reveals the one-time code field when the bridge asks for a second factor", async () => {
    mockSignIn.mockResolvedValue({ ok: false, code: "mfa_required" } as never);
    render(<LocalSignInForm callbackUrl="/" />);
    fill();
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    expect(await screen.findByLabelText(/one-time code/i)).toBeInTheDocument();
    expect(screen.getByText(/authenticator/i)).toBeInTheDocument();
  });

  it("keeps what was typed so the second step does not mean retyping the password", async () => {
    mockSignIn.mockResolvedValue({ ok: false, code: "mfa_required" } as never);
    render(<LocalSignInForm callbackUrl="/" />);
    fill();
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
    await screen.findByLabelText(/one-time code/i);

    expect((screen.getByLabelText(/email/i) as HTMLInputElement).value).toBe("alice@example.com");
    expect((screen.getByLabelText(/^password$/i) as HTMLInputElement).value).toBe(
      "correct horse battery",
    );

    mockSignIn.mockResolvedValue({ ok: true } as never);
    fireEvent.change(screen.getByLabelText(/one-time code/i), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
    await waitFor(() => expect(mockSignIn).toHaveBeenCalledTimes(2));
    expect(mockSignIn.mock.calls[1][1]).toMatchObject({ code: "123456" });
  });

  it("shows one generic message for every other failure", async () => {
    mockSignIn.mockResolvedValue({ ok: false, code: "credentials" } as never);
    render(<LocalSignInForm callbackUrl="/" />);
    fill();
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/email or password/i);
    expect(screen.queryByLabelText(/one-time code/i)).not.toBeInTheDocument();
  });

  it("never renders the submitted password into the page text", async () => {
    mockSignIn.mockResolvedValue({ ok: false, code: "credentials" } as never);
    const { container } = render(<LocalSignInForm callbackUrl="/" />);
    fill("alice@example.com", "hunter2-hunter2");
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
    await screen.findByRole("alert");
    expect(container.textContent).not.toContain("hunter2");
  });

  it("marks the password field as a password so browsers never show it", () => {
    render(<LocalSignInForm callbackUrl="/" />);
    expect(screen.getByLabelText(/^password$/i)).toHaveAttribute("type", "password");
  });

  it.each([
    "https://evil.example.com/",
    "//evil.example.com/",
    "http://evil.example.com",
    "javascript:alert(1)",
    "/\\evil.example.com",
  ])("never navigates off-origin after sign-in (%s)", async (hostile) => {
    // `callbackUrl` arrives from the query string, so /signin?callbackUrl=...
    // is attacker-controlled. A post-sign-in window.location.assign() of that
    // value is a textbook open redirect — and a very convincing one, because
    // the victim has just typed their password.
    mockSignIn.mockResolvedValue({ ok: true } as never);
    const assign = vi.fn();
    Object.defineProperty(window, "location", {
      value: { assign, href: "http://localhost/" },
      writable: true,
    });
    render(<LocalSignInForm callbackUrl={hostile} />);
    fill();
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
    await waitFor(() => expect(assign).toHaveBeenCalled());
    expect(assign).toHaveBeenCalledWith("/");
  });

  it("keeps a legitimate relative callback", async () => {
    mockSignIn.mockResolvedValue({ ok: true } as never);
    const assign = vi.fn();
    Object.defineProperty(window, "location", {
      value: { assign, href: "http://localhost/" },
      writable: true,
    });
    render(<LocalSignInForm callbackUrl="/runs?id=7" />);
    fill();
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/runs?id=7"));
  });

  it("does not submit an empty form", async () => {
    render(<LocalSignInForm callbackUrl="/" />);
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
    await waitFor(() => expect(mockSignIn).not.toHaveBeenCalled());
  });
});
