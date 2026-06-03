import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "PoiesisPathfinder",
  description: "Brutalist telemetry dashboard for the PoiesisPathfinder AI gateway.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
