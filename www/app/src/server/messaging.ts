import axios from "axios";

import {
  Aliases,
  LanguageSettings,
  NetworkSettings,
  PluginSettings,
} from "../context/configuration";
import { AxiosError } from "axios/index";

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
  message_history: Message[];
  // The credentials of the user
  network_settings: NetworkSettings;
  // The server to connect to for the backend and the settings to use
  language_settings: LanguageSettings;
  // The plugin list to use TODO: Add this to the configuration
  plugin_list?: PluginSettings[];
  // The aliases to use, if any are defined
  aliases?: Aliases;
}

// sendMessage will reformat the userMessage and send it to the Server URL with
// the provided auth token. It will return the response from the server.
// If the response is undefined, then the server did not respond.
export const sendMessage = async (
  serverURL: string,
  userMessage: UserMessage,
  authToken: string,
): Promise<BotMessage | undefined> => {
  // Strip the userMessage history of all "code" message sections
  userMessage.message_history = userMessage.message_history.map((message) => {
    return {
      ...message,
      sections: message.sections.filter(
        (section) => section.messageType !== "code",
      ),
    };
  });
  const response = await axios
    .post<BotMessage>(`${serverURL}/chat/message`, userMessage, {
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + authToken,
      },
      timeout: 60000,
    })
    .catch((error) => {
      if (axios.isAxiosError(error)) {
        const axiosError = error as AxiosError;
        if (axiosError.response) {
          // The request was made, but the server responded with a status code outside the 2xx range
          throw new Error(axiosError.response.statusText);
        }
      }
      return undefined;
    })
    .then((response) => {
      if (response) {
        try {
          return response.data as BotMessage;
        } catch (error) {
          console.error(error);
        }
        return undefined;
      }
    });
  console.log("Received response from server");
  return response;
};

export const getGreeting = async (
  serverURL: string,
  authToken: string,
): Promise<BotMessage | null> => {
  const response = await axios
    .get<BotMessage>(`${serverURL}/chat/greeting`, {
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + authToken,
      },
      timeout: 60000,
    })
    .then((response) => {
      if (response) {
        return response.data;
      }
    });
  console.log("Received response from server");
  return response ?? null;
};
