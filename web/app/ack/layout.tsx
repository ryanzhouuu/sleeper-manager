import type { Metadata } from "next";
import type { ReactNode } from "react";
import { ControlsShell } from "@/components/controls-shell";

export const metadata: Metadata = {
  title: "Confirm decision · Lock In",
  robots: { index: false, follow: false },
  referrer: "same-origin",
};

export default function AckLayout({ children }: { children: ReactNode }) {
  return <ControlsShell>{children}</ControlsShell>;
}
