import type { SettingDefinition } from "../../../types";

const safetyThresholdOptions = [
  { value: "0", label: "Off" },
  { value: "1", label: "Block none" },
  { value: "2", label: "Block few" },
  { value: "3", label: "Block some" },
  { value: "4", label: "Block most" },
] as const;

function createSafetyThresholdSetting(
  id: string,
  displayName: string,
  description: string,
): SettingDefinition {
  return {
    id,
    displayName,
    description,
    valueType: "number",
    control: "slider",
    defaultValue: 0,
    min: 0,
    max: 4,
    step: 1,
    options: safetyThresholdOptions,
  };
}

export const googleSettingCatalog: Record<string, SettingDefinition> = {
  googleMapsGrounding: {
    id: "googleMapsGrounding",
    displayName: "Google Maps Grounding",
    description: "Enables Grounding with Google Maps when the model supports it.",
    valueType: "boolean",
    control: "toggle",
    defaultValue: false,
  },
  urlContext: {
    id: "urlContext",
    displayName: "URL Context",
    description: "Lets the model read and ground responses in provided URLs.",
    valueType: "boolean",
    control: "toggle",
    defaultValue: false,
  },
  mediaResolution: {
    id: "mediaResolution",
    displayName: "Media Resolution",
    description: "Controls how much detail the model uses when processing media inputs.",
    valueType: "enum",
    control: "select",
    defaultValue: "MEDIA_RESOLUTION_UNSPECIFIED",
    options: [
      { value: "MEDIA_RESOLUTION_UNSPECIFIED", label: "Default" },
      { value: "MEDIA_RESOLUTION_LOW", label: "Low" },
      { value: "MEDIA_RESOLUTION_MEDIUM", label: "Medium" },
      { value: "MEDIA_RESOLUTION_HIGH", label: "High" },
    ],
  },
  harassmentSafety: createSafetyThresholdSetting(
    "harassmentSafety",
    "Harassment",
    "Controls the block threshold for harassment content.",
  ),
  hateSpeechSafety: createSafetyThresholdSetting(
    "hateSpeechSafety",
    "Hate Speech",
    "Controls the block threshold for hate speech content.",
  ),
  sexuallyExplicitSafety: createSafetyThresholdSetting(
    "sexuallyExplicitSafety",
    "Sexually Explicit",
    "Controls the block threshold for sexually explicit content.",
  ),
  dangerousContentSafety: createSafetyThresholdSetting(
    "dangerousContentSafety",
    "Dangerous Content",
    "Controls the block threshold for dangerous content.",
  ),
  responseMimeType: {
    id: "responseMimeType",
    displayName: "Response Format",
    description: "Hints the provider about the output format you want back.",
    valueType: "enum",
    control: "select",
    defaultValue: "text/plain",
    options: [
      { value: "text/plain", label: "Plain Text" },
      { value: "application/json", label: "JSON" },
      { value: "text/x.enum", label: "Enum Text" },
    ],
  },
  embeddingTaskType: {
    id: "embeddingTaskType",
    displayName: "Embedding Task Type",
    description: "Tunes the embedding for the kind of retrieval or comparison you want.",
    valueType: "enum",
    control: "select",
    defaultValue: "TASK_TYPE_UNSPECIFIED",
    options: [
      { value: "TASK_TYPE_UNSPECIFIED", label: "Model Default" },
      { value: "SEMANTIC_SIMILARITY", label: "Semantic Similarity" },
      { value: "RETRIEVAL_QUERY", label: "Retrieval Query" },
      { value: "RETRIEVAL_DOCUMENT", label: "Retrieval Document" },
      { value: "CLASSIFICATION", label: "Classification" },
      { value: "CLUSTERING", label: "Clustering" },
      { value: "QUESTION_ANSWERING", label: "Question Answering" },
      { value: "FACT_VERIFICATION", label: "Fact Verification" },
    ],
  },
  embeddingTitle: {
    id: "embeddingTitle",
    displayName: "Embedding Title",
    description:
      "Optional title to improve retrieval quality for RETRIEVAL_DOCUMENT embedding tasks.",
    valueType: "string",
    control: "input",
    defaultValue: "",
  },
};

export function getGoogleSettingDefinition(settingId: string): SettingDefinition | undefined {
  return googleSettingCatalog[settingId];
}
