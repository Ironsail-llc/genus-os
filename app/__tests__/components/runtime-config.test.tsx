import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  RuntimeConfigProvider,
  useRuntimeConfig,
} from "@/components/runtime-config";

function ConfigConsumer() {
  const { aiName } = useRuntimeConfig();
  return <span>{aiName}</span>;
}

describe("RuntimeConfigProvider", () => {
  it("provides only the explicitly public request-time configuration", () => {
    render(
      <RuntimeConfigProvider aiName="Genus">
        <ConfigConsumer />
      </RuntimeConfigProvider>,
    );

    expect(screen.getByText("Genus")).toBeInTheDocument();
  });

  it("uses a safe default outside the application root", () => {
    render(<ConfigConsumer />);
    expect(screen.getByText("Robothor")).toBeInTheDocument();
  });
});
