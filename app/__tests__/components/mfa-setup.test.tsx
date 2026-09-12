import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AccountSecurityPanel } from "@/components/account-security-panel";
import { MfaSetupBanner } from "@/components/mfa-setup-banner";

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("MfaSetupBanner", () => {
  it("appears when /api/auth/me says the owner still owes a second factor", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      jsonResponse({ role: "owner", mfa_enabled: false, mfa_setup_required: true }),
    );
    render(<MfaSetupBanner />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Owner accounts must enable two-factor authentication/i,
    );
    expect(screen.getByRole("link", { name: /two-factor|security/i })).toHaveAttribute(
      "href",
      "/account/security",
    );
  });

  it("offers no way to dismiss it", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      jsonResponse({ role: "owner", mfa_enabled: false, mfa_setup_required: true }),
    );
    render(<MfaSetupBanner />);
    await screen.findByRole("alert");
    expect(screen.queryByRole("button", { name: /dismiss|close|later|got it/i })).toBeNull();
  });

  it("stays hidden once MFA is on, and when the endpoint is unreachable", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      jsonResponse({ role: "owner", mfa_enabled: true, mfa_setup_required: false }),
    );
    const { container, unmount } = render(<MfaSetupBanner />);
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(container.textContent).not.toMatch(/two-factor/i);
    unmount();

    vi.spyOn(global, "fetch").mockRejectedValue(new Error("offline"));
    const offline = render(<MfaSetupBanner />);
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(offline.container.textContent).toBe("");
  });
});

describe("AccountSecurityPanel", () => {
  it("enrolls, shows the URI and secret as selectable text, then confirms", async () => {
    const fetchMock = vi
      .spyOn(global, "fetch")
      .mockResolvedValueOnce(
        jsonResponse({ role: "owner", mfa_enabled: false, mfa_setup_required: true }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          secret: "JBSWY3DPEHPK3PXP",
          otpauth_uri: "otpauth://totp/Genus%20OS:alice@example.com?secret=JBSWY3DPEHPK3PXP",
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ success: true }));

    render(<AccountSecurityPanel />);
    fireEvent.change(await screen.findByLabelText(/confirm your password/i), {
      target: { value: "correct horse battery" },
    });
    fireEvent.click(screen.getByRole("button", { name: /enable two-factor/i }));

    // The base32 secret AND the otpauth:// URI are both shown as selectable
    // text — there is no QR dependency in this bundle, and every authenticator
    // accepts a typed secret.
    expect(await screen.findByText("JBSWY3DPEHPK3PXP")).toBeInTheDocument();
    expect(
      screen.getByText("otpauth://totp/Genus%20OS:alice@example.com?secret=JBSWY3DPEHPK3PXP"),
    ).toBeInTheDocument();
    expect(fetchMock.mock.calls[1][0]).toBe("/api/bridge/api/auth/mfa/enroll");

    fireEvent.change(screen.getByLabelText(/one-time code/i), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(fetchMock.mock.calls[2][0]).toBe("/api/bridge/api/auth/mfa/confirm");
    expect(JSON.parse((fetchMock.mock.calls[2][1] as RequestInit).body as string)).toEqual({
      code: "123456",
    });
    expect(await screen.findByText(/two-factor is on/i)).toBeInTheDocument();
  });

  it("reports a rejected code without enabling anything", async () => {
    vi.spyOn(global, "fetch")
      .mockResolvedValueOnce(
        jsonResponse({ role: "owner", mfa_enabled: false, mfa_setup_required: true }),
      )
      .mockResolvedValueOnce(jsonResponse({ secret: "S", otpauth_uri: "otpauth://totp/x" }))
      .mockResolvedValueOnce(jsonResponse({ error: "invalid credentials" }, 401));

    render(<AccountSecurityPanel />);
    fireEvent.change(await screen.findByLabelText(/confirm your password/i), {
      target: { value: "correct horse battery" },
    });
    fireEvent.click(screen.getByRole("button", { name: /enable two-factor/i }));
    fireEvent.change(await screen.findByLabelText(/one-time code/i), {
      target: { value: "000000" },
    });
    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/code/i);
    expect(screen.queryByText(/two-factor is on/i)).toBeNull();
  });

  it("changes a password and never renders either one back", async () => {
    const fetchMock = vi
      .spyOn(global, "fetch")
      .mockResolvedValueOnce(
        jsonResponse({ role: "owner", mfa_enabled: true, mfa_setup_required: false }),
      )
      .mockResolvedValueOnce(jsonResponse({ success: true }));

    const { container } = render(<AccountSecurityPanel />);
    fireEvent.change(await screen.findByLabelText(/current password/i), {
      target: { value: "hunter2-hunter2" },
    });
    fireEvent.change(screen.getByLabelText(/new password/i), {
      target: { value: "a-much-longer-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: /change password/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[1][0]).toBe("/api/account/password");
    expect(await screen.findByText(/password changed/i)).toBeInTheDocument();
    expect(container.textContent).not.toContain("hunter2");
    expect(container.textContent).not.toContain("a-much-longer-password");
  });

  it("refuses a new password under twelve characters before calling the bridge", async () => {
    const fetchMock = vi
      .spyOn(global, "fetch")
      .mockResolvedValue(jsonResponse({ role: "owner", mfa_enabled: true }));

    render(<AccountSecurityPanel />);
    fireEvent.change(await screen.findByLabelText(/current password/i), {
      target: { value: "hunter2-hunter2" },
    });
    fireEvent.change(screen.getByLabelText(/new password/i), { target: { value: "short" } });
    fireEvent.click(screen.getByRole("button", { name: /change password/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/12/);
    expect(fetchMock).toHaveBeenCalledTimes(1); // the /me load only
  });

  it("sends the current password with the enrolment request", async () => {
    const fetchMock = vi
      .spyOn(global, "fetch")
      .mockResolvedValueOnce(
        jsonResponse({ role: "owner", mfa_enabled: false, mfa_setup_required: true }),
      )
      .mockResolvedValueOnce(jsonResponse({ secret: "S", otpauth_uri: "otpauth://totp/x" }));

    render(<AccountSecurityPanel />);
    fireEvent.change(await screen.findByLabelText(/confirm your password/i), {
      target: { value: "correct horse battery" },
    });
    fireEvent.click(screen.getByRole("button", { name: /enable two-factor/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const body = JSON.parse((fetchMock.mock.calls[1][1] as RequestInit).body as string);
    expect(body).toEqual({ password: "correct horse battery" });
  });

  it("will not start an enrolment with no password typed", async () => {
    const fetchMock = vi
      .spyOn(global, "fetch")
      .mockResolvedValue(
        jsonResponse({ role: "owner", mfa_enabled: false, mfa_setup_required: true }),
      );
    render(<AccountSecurityPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /enable two-factor/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  });

  it("reports a refused enrolment password without revealing anything else", async () => {
    vi.spyOn(global, "fetch")
      .mockResolvedValueOnce(
        jsonResponse({ role: "owner", mfa_enabled: false, mfa_setup_required: true }),
      )
      .mockResolvedValueOnce(jsonResponse({ error: "invalid credentials" }, 401));

    render(<AccountSecurityPanel />);
    fireEvent.change(await screen.findByLabelText(/confirm your password/i), {
      target: { value: "hunter2-hunter2" },
    });
    fireEvent.click(screen.getByRole("button", { name: /enable two-factor/i }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/password/i);
    expect(alert.textContent).not.toContain("hunter2");
    expect(screen.queryByLabelText(/one-time code/i)).toBeNull();
  });

  it("never renders the enrolment password back into the page", async () => {
    vi.spyOn(global, "fetch")
      .mockResolvedValueOnce(
        jsonResponse({ role: "owner", mfa_enabled: false, mfa_setup_required: true }),
      )
      .mockResolvedValueOnce(jsonResponse({ secret: "S", otpauth_uri: "otpauth://totp/x" }));

    const { container } = render(<AccountSecurityPanel />);
    fireEvent.change(await screen.findByLabelText(/confirm your password/i), {
      target: { value: "hunter2-hunter2" },
    });
    fireEvent.click(screen.getByRole("button", { name: /enable two-factor/i }));
    await screen.findByLabelText(/one-time code/i);
    expect(container.textContent).not.toContain("hunter2");
  });
});
