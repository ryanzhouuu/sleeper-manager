/** All private browser pages share the dashboard typography and navigation. */
import Link from "next/link";
import type { ReactNode } from "react";
import "./controls.css";

export function ControlsShell({ children }: { children: ReactNode }) {
  return (
    <main className="page controls-page">
      <nav aria-label="Back to dashboard">
        <Link className="controls-link" href="/">
          ← Dashboard
        </Link>
      </nav>
      {children}
    </main>
  );
}
