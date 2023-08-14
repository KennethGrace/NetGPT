import axios, { AxiosError, AxiosResponse } from "axios";

import {
  NetworkSettings,
  Aliases,
  LanguageSettings,
} from "../common/configuration";

export type SenderType = "NetGPT" | "You";

export type MessageType = "text" | "code" | "error";

export interface MessageSection {
  // The type of the message
  messageType: MessageType;
  // The content of the message
  content: string;
}

export interface Message {
  // Sender of the message
  sender: SenderType;
  // The message content
  sections: MessageSection[];
  // Time since the Epoch in seconds
  timestamp: number;
}

export interface BotMessage extends Message {
  // An optional caption for the message
  caption?: string;
}

export interface UserMessage {
  // The message history of the user
  messageHistory: Message[];
  // The credentials of the user
  connectionParameters: NetworkSettings;
  // The server to connect to for the backend and the settings to use
  languageSettings: LanguageSettings;
  // The aliases to use, if any are defined
  aliases?: Aliases;
}

export const sendMessage = async (
  serverURL: string,
  userMessage: UserMessage
): Promise<BotMessage | undefined> => {
  console.log("Sending message to server");
  console.log(userMessage);
  // Strip the userMessage history of all "code" message sections
  userMessage.messageHistory = userMessage.messageHistory.map((message) => {
    return {
      ...message,
      sections: message.sections.filter(
        (section) => section.messageType !== "code"
      ),
    };
  });
  const response = await axios
    .post<BotMessage>(`${serverURL}/chat/message`, userMessage, {
      headers: {
        "Content-Type": "application/json",
      },
      timeout: 60000,
    }).then((response) => {
      if (response) {
        return response.data;
      }
    });
  console.log("Received response from server");
  return response;
};
