"use client";

import { createContext, useContext, type ReactNode } from "react";

interface RuntimeConfig {
  aiName: string;
}

const DEFAULT_RUNTIME_CONFIG: RuntimeConfig = {
  aiName: "Robothor",
};

const RuntimeConfigContext = createContext<RuntimeConfig>(DEFAULT_RUNTIME_CONFIG);

export function RuntimeConfigProvider({
  aiName,
  children,
}: RuntimeConfig & { children: ReactNode }) {
  return (
    <RuntimeConfigContext.Provider value={{ aiName }}>
      {children}
    </RuntimeConfigContext.Provider>
  );
}

export function useRuntimeConfig(): RuntimeConfig {
  return useContext(RuntimeConfigContext);
}
