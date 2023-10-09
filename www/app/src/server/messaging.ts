import axios, {AxiosError} from "axios";

import {Aliases, LanguageSettings, NetworkSettings, PluginSettings,} from "../context/configuration";

// The SenderType enum instructs the frontend on how to render the message.
export type SenderType = "NetGPT" | "You";

// The MessageType enum instructs the frontend on how to render the message.
export type MessageType = "text" | "code" | "error" | "procedural";

// The MessageSection interface is used to represent a section of a message.
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
  // Strip the userMessage history of all "code" message sections to reduce size and load on the server
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
  // For every "text" message section, we want to parse the Markdown content into
  // MessageSection objects. For instance if the message content is a code block,
  // we want to parse it into a MessageSection with messageType "code".
  // Therefore, we will need to yield multiple MessageSection objects for each existing MessageSection.
  // For ease of use, we will just build a new array of MessageSection objects and replace the old one.
  const formattedSections: MessageSection[] = [];
  response?.sections.map((section) => {
    if (section.messageType === "text") {
      // Parse the Markdown content
      const parsedSections = parseMarkdown(section.content);
      // Add the parsed sections to the formatted response
      formattedSections.push(...parsedSections);
    } else {
      // If the message type is not "text", we can just add it to the formatted response
      formattedSections.push(section);
    }
  });
  response!.sections = formattedSections;
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

// parseMarkdown will parse the markdown string into an array of MessageSection objects.
// For every "text" message section, we want to parse the Markdown content into
// MessageSection objects. For instance if the message content is a code block,
// we want to parse it into a MessageSection with messageType "code".
const parseMarkdown = (markdown: string): MessageSection[] => {
  // We create a new array of MessageSection objects to return once we are done parsing,
  // for building sections we make a reference to the last section in the array.
  const formattedSections: MessageSection[] = [];
  // We get a function for building sections
  const buildSection = getSectionBuilder(formattedSections);
  // We get a function for checking the message type
  const checkMessageType = getMessageChecker();

  // Split the markdown string into its lines
  const lines = markdown.split("\n");
  // For every line, we want to parse the Markdown content
  lines.map((line) => {
    switch (checkMessageType(line)) {
      case "text":
        // If the line is text, we want to add it to the current section
        buildSection("text").content += line + "\n";
        break;
      case "code":
        // If the line is code, we want to add it to the current section
        buildSection("code").content += line + "\n";
        break;
      case null:
        // If the line is a syntax indicator, we want to ignore it
        break;
    }
  });
  // Add the last section to the formattedSections array
  formattedSections.push(buildSection("text"));
  return formattedSections;
};

// getSectionBuilder will return a function for building sections, it will return the current section being built,
// unless the messageType changes, in which case it will add the current section to the formattedSections array,
// and start and return a new section.
const getSectionBuilder = (
  formattedSections: MessageSection[],
): ((messageType: MessageType) => MessageSection) => {
  let currentSection: MessageSection = {
    messageType: "text",
    content: "",
  };
  return (messageType: MessageType): MessageSection => {
    if (currentSection.messageType !== messageType) {
      // If the messageType changes, we want to add the current section to the formattedSections array
      formattedSections.push(currentSection);
      // And start a new section
      currentSection = {
        messageType: messageType,
        content: "",
      };
    }
    return currentSection;
  };
};

const getMessageChecker = (): ((line: string) => MessageType | null) => {
  let messageState: MessageType = "text";
  // toggleCodeBlock will toggle the code block state, whenever a code block indicator is found
  // the code block state will be set true, and when another code block indicator is found,
  // the code block state will be set false. The section being built will be added to the
  // formattedSections array, and a new section will be started.
  return (line: string): MessageType | null => {
    let l = line.trim();
    // If the line is empty, we want to return null
    if (l === "") {
      return null;
    }
    if (l.startsWith("```")) {
      // If the line is a code block indicator, we want to toggle the code block state
      // If we are already in a code block, we want to end it and vice versa, but
      // we should ignore the indicators themselves, so we return null. On the next
      // line, the code block state will be accurate.
      messageState = messageState === "text" ? "code" : "text";
      return null;
    }
    // If the line begins with a digit with a period, we want to set the message type to "procedural"
    return messageState;
  };
};
