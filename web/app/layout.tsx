import "@hackerrank/hrds-styles-v3/dist/main-with-theme.css";
import "./globals.css";

import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import type { ReactNode } from "react";

import { Providers } from "./providers";

// ADE's design tokens name Geist / Geist Mono but ship no font files; next/font
// self-hosts them into the export so the app never depends on a CDN at runtime.
const sans = Geist({ subsets: ["latin"], variable: "--font-geist-sans", display: "swap" });
const mono = Geist_Mono({ subsets: ["latin"], variable: "--font-geist-mono", display: "swap" });

export const metadata: Metadata = {
  title: "pair review",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${sans.variable} ${mono.variable}`}>
      {/* HRThemeProvider sets these in an effect; setting them here avoids a light flash on load. */}
      <body data-theme="dark" data-theme-version="3" suppressHydrationWarning>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
