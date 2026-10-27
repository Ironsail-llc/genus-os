"use client";

import { createContext, useContext, type ReactNode } from "react";

import { DEFAULT_MARKETPLACE_URL } from "@/lib/marketplace-url";

interface RuntimeConfig {
  aiName: string;
  marketplaceUrl: string;
}

const DEFAULT_RUNTIME_CONFIG: RuntimeConfig = {
  aiName: "Robothor",
  marketplaceUrl: DEFAULT_MARKETPLACE_URL,
};

const RuntimeConfigContext = createContext<RuntimeConfig>(DEFAULT_RUNTIME_CONFIG);

export function RuntimeConfigProvider({
  aiName,
  marketplaceUrl = DEFAULT_MARKETPLACE_URL,
  children,
}: Pick<RuntimeConfig, "aiName"> &
  Partial<Pick<RuntimeConfig, "marketplaceUrl">> & { children: ReactNode }) {
  return (
    <RuntimeConfigContext.Provider value={{ aiName, marketplaceUrl }}>
      {children}
    </RuntimeConfigContext.Provider>
  );
}

export function useRuntimeConfig(): RuntimeConfig {
  return useContext(RuntimeConfigContext);
}
