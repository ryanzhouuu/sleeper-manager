import type { Metadata } from "next";
import type { ReactNode } from "react";
import "@fontsource/iosevka/400.css";
import "@fontsource/iosevka/500.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "Lock In",
  description: "Current matchup, roster, games, and lineup recommendation.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
