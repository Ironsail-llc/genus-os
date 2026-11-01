import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { SessionProvider } from "next-auth/react";
import { RuntimeConfigProvider } from "@/components/runtime-config";
import { marketplaceUrl } from "@/lib/marketplace-url";
import { ThemeProvider } from "@/components/theme-provider";
import { TooltipProvider } from "@/components/ui/tooltip";
import { WebVitals } from "@/components/web-vitals";
import { webVitalsEnabled } from "@/lib/web-vitals";
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
  const marketplace = marketplaceUrl(runtimeValue("NEXT_PUBLIC_PROGRAMMATIC_RESOURCES_URL"));

  return (
    <html lang="en" suppressHydrationWarning>
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        {webVitalsEnabled() && <WebVitals />}
        <ThemeProvider>
          <SessionProvider>
            <RuntimeConfigProvider aiName={aiName} marketplaceUrl={marketplace}>
              <TooltipProvider>{children}</TooltipProvider>
            </RuntimeConfigProvider>
          </SessionProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
