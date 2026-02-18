"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";

interface FormField {
  name: string;
  label: string;
  type?: string;
  placeholder?: string;
  required?: boolean;
}

interface FormPanelProps {
  title: string;
  fields: FormField[];
  onSubmit: (data: Record<string, string>) => void;
  submitLabel?: string;
}

export function FormPanel({
  title,
  fields,
  onSubmit,
  submitLabel = "Submit",
}: FormPanelProps) {
  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const formData = new FormData(e.currentTarget);
    const data: Record<string, string> = {};
    fields.forEach((field) => {
      data[field.name] = (formData.get(field.name) as string) || "";
    });
    onSubmit(data);
  };

  return (
    <Card className="glass-panel" data-testid="form-panel">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="space-y-4">
          {fields.map((field) => (
            <div key={field.name} className="space-y-1">
              <label className="text-sm text-muted-foreground">
                {field.label}
                {field.required && <span className="text-red-400 ml-1">*</span>}
              </label>
              <Input
                name={field.name}
                type={field.type || "text"}
                placeholder={field.placeholder}
                required={field.required}
                data-testid={`field-${field.name}`}
              />
            </div>
          ))}
          <Button type="submit" data-testid="form-submit">
            {submitLabel}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
