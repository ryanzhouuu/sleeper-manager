import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import fixture from "@/test/fixtures/controls.json";
import { parseControlDocument } from "@/lib/control-document";
import type {
  ConfirmationDocument,
  ManagerDocument,
} from "@/lib/control-types";
import { ConfirmationScreen } from "./confirmation-screen";
import { DecisionEditor } from "./decision-editor";
import { ManagerScreen } from "./manager-screen";
import { ControlStatus } from "./control-status";

vi.mock("next/image", () => ({
  default: ({ src, alt }: { src: string; alt: string }) => (
    // eslint-disable-next-line @next/next/no-img-element -- replaces the image optimizer in markup tests
    <img src={src} alt={alt} />
  ),
}));

const document = parseControlDocument(fixture) as ManagerDocument;
const preview: ConfirmationDocument = {
  status: "confirmation",
  heading: "Confirm your Sleeper lock",
  title: "Example <player>",
  message: "<script>untrusted</script>",
  instruction: "Save only after locking this game in Sleeper.",
  label: "I locked this game in Sleeper",
  token: "test-token",
  action: "locked",
};

function editor(
  changes: Partial<ManagerDocument> = {},
  dirty = false,
  paused = false,
) {
  return renderToStaticMarkup(
    <DecisionEditor
      document={{ ...document, ...changes }}
      busy={false}
      dirty={dirty}
      paused={paused}
      onEdit={vi.fn()}
      onSubmit={vi.fn()}
      onRefresh={vi.fn()}
    />,
  );
}

describe("consolidated control pages", () => {
  it("keeps the password in a POST form even before JavaScript loads", () => {
    const html = renderToStaticMarkup(
      <ManagerScreen initial={{ status: "login", rejected: false }} />,
    );
    expect(html.match(/<form[^>]*>/)?.[0]).toContain('method="post"');
    expect(html).toContain('action="/manage/api/login"');
    expect(html).toContain('type="password"');
    expect(html).not.toContain("worker.test");
  });

  it("preserves batch versions and independent Plan/Saved choices", () => {
    const html = editor();
    expect(html.match(/<form[^>]*>/)?.[0]).toContain('method="post"');
    expect(html).toContain('action="/manage/api/save"');
    expect(html).toContain('name="version_0" value="0"');
    expect(html).toContain('name="action_0" checked="" value="not_locked"');
    expect(html).toContain('aria-label="Advisor: Pass"');
    expect(html).toContain('aria-label="Recorded: Wait"');
    expect(html).toContain("32.80");
    expect(html).toContain("Save &amp; replan");
  });

  it("shows a loading status and keeps stale advice hidden while replanning", () => {
    const html = editor({ pending: true, plan: null });
    expect(html).toContain('role="status"');
    expect(html).toContain("Updating your plan…");
    expect(html).not.toContain("Team plan");
    expect(html).not.toContain("Retry plan");
    expect(editor({ pending: true, plan: null }, true)).toContain(
      "Your unsaved choices are kept",
    );
    expect(editor({ pending: true, plan: null }, false, true)).toContain(
      "Check again",
    );
  });

  it("keeps saved corrections and automatic history separate", () => {
    const html = editor({
      active: [],
      saved: [
        { ...document.active[0], action: "locked", saved: "Locked in Sleeper" },
      ],
      past: [
        {
          name: "Earlier game",
          score: 12,
          start: fixture.active[0].start,
          state: "Passed · eligibility lost",
        },
      ],
    });
    expect(html).toContain("Saved decisions · 1");
    expect(html).toContain("Edit to correct a saved decision.");
    expect(html).toContain("Passed · eligibility lost");
    expect(html.match(/name="action_0"/g)).toHaveLength(3);
    expect(html).not.toContain('name="action_1"');
  });

  it("requires an explicit same-origin confirmation form and escapes advice", () => {
    const html = renderToStaticMarkup(<ConfirmationScreen initial={preview} />);
    expect(html.match(/<form[^>]*>/)?.[0]).toContain('method="post"');
    expect(html).toContain('action="/ack/submit"');
    expect(html).toContain('name="token" value="test-token"');
    expect(html).toContain("&lt;script&gt;untrusted&lt;/script&gt;");
    expect(html).not.toContain("<script>");
    expect(html).toContain('href="/manage"');
  });

  it("does not offer a confirmation form for expired or replaced links", () => {
    const html = renderToStaticMarkup(
      <ConfirmationScreen
        initial={{ status: "message", title: "Confirmation expired" }}
      />,
    );
    expect(html).toContain("Confirmation expired");
    expect(html).not.toContain("<form");
  });

  it("announces loading while hiding the decorative animation from assistive technology", () => {
    expect(
      renderToStaticMarkup(<ControlStatus>Saving decisions…</ControlStatus>),
    ).toContain(
      'role="status"><span class="control-spinner" aria-hidden="true"',
    );
  });
});
