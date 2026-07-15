import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { SessionProvider } from "next-auth/react";
import { RuntimeConfigProvider } from "@/components/runtime-config";
import { TooltipProvider } from "@/components/ui/tooltip";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
};

export const metadata: Metadata = {
  title: "Genus OS",
  description: "Genus OS Business Layer",
  icons: {
    icon: "/robothor-bolt.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // Indirect lookup keeps this operator-owned value request-time configurable
  // in the standalone image instead of baking it into the browser bundle.
  const runtimeValue = (name: string) => process.env[name];
  const aiName = runtimeValue("NEXT_PUBLIC_AI_NAME") || "Robothor";

  return (
    <html lang="en" className="dark">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        <SessionProvider>
          <RuntimeConfigProvider aiName={aiName}>
            <TooltipProvider>{children}</TooltipProvider>
          </RuntimeConfigProvider>
        </SessionProvider>
      </body>
    </html>
  );
}
