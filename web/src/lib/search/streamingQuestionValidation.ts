import { AnswerPiecePacket, ValidQuestionResponse } from "./interfaces";
import { processRawChunkString } from "./streamingUtils";

export interface QuestionValidationArgs {
  query: string;
  update: (update: Partial<ValidQuestionResponse>) => void;
}

export const questionValidationStreamed = async <T>({
  query,
  update,
}: QuestionValidationArgs) => {
  const emptyFilters = {
    source_type: null,
    document_set: null,
    time_cutoff: null,
  };

  const response = await fetch("/api/stream-query-validation", {
    method: "POST",
    body: JSON.stringify({
      query,
      collection: "danswer_index",
      filters: emptyFilters,
      enable_auto_detect_filters: false,
      offset: null,
    }),
    headers: {
      "Content-Type": "application/json",
    },
  });
  const reader = response.body?.getReader();
  const decoder = new TextDecoder("utf-8");

  let reasoning = "";
  let previousPartialChunk: string | null = null;
  while (true) {
    const rawChunk = await reader?.read();
    if (!rawChunk) {
      throw new Error("Unable to process chunk");
    }
    const { done, value } = rawChunk;
    if (done) {
      break;
    }

    const [completedChunks, partialChunk] = processRawChunkString<
      AnswerPiecePacket | ValidQuestionResponse
    >(decoder.decode(value, { stream: true }), previousPartialChunk);
    if (!completedChunks.length && !partialChunk) {
      break;
    }
    previousPartialChunk = partialChunk as string | null;

    completedChunks.forEach((chunk) => {
      if (Object.hasOwn(chunk, "answer_piece")) {
        reasoning += (chunk as AnswerPiecePacket).answer_piece;
        update({
          reasoning,
        });
      }

      if (Object.hasOwn(chunk, "answerable")) {
        update({ answerable: (chunk as ValidQuestionResponse).answerable });
      }
    });
  }
};
