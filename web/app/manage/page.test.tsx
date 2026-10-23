import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cookies } from "next/headers";
import { loadControl } from "@/lib/control-backend";
import ManagePage from "./page";

vi.mock("next/headers", () => ({ cookies: vi.fn() }));
vi.mock("@/lib/control-backend", () => ({ loadControl: vi.fn() }));

describe("owner page", () => {
  beforeEach(() => {
    vi.mocked(loadControl).mockResolvedValue({
      status: "login",
      rejected: false,
    });
    vi.mocked(cookies).mockResolvedValue({
      get: vi
        .fn()
        .mockReturnValue({ name: "manager_session", value: "123.abc.def" }),
    } as unknown as Awaited<ReturnType<typeof cookies>>);
  });

  it("reads the owner session on the server and renders an app-origin login", async () => {
    const html = renderToStaticMarkup(await ManagePage());
    expect(loadControl).toHaveBeenCalledWith(
      "/manage",
      "manager_session=123.abc.def",
    );
    expect(html).toContain('action="/manage/api/login"');
    expect(html).not.toContain("123.abc.def");
  });
});
