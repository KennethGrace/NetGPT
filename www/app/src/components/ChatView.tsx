import React, { useState, FC, lazy, Suspense, useMemo } from "react";

import {
  Box,
  Container,
  List,
  ListItem,
  Typography,
  Paper,
  Stack,
  useTheme,
} from "@mui/material";

import {
  UserMessage,
  Message,
  sendMessage,
  BotMessage,
  MessageType,
  SenderType,
} from "../server/messaging";
import { Notification } from "../common/Notify";

import defaultMessages from "../json/defaultMessages.json";
import { useConfiguration } from "../common/configuration";
import axios from "axios";

const InputBox = lazy(() => import("./InputBox"));

type Justification = "right" | "left";

const ChatMessageSection: FC<{
  senderType: SenderType;
  messageType: MessageType;
  content: string;
  justification: Justification;
}> = ({ senderType, messageType, content, justification }) => {
  const theme = useTheme();
  const messageAttrs = useMemo(() => {
    if (messageType === "code") {
      return {
        maxHeight: "160px",
        overflow: "auto",
        border: "1px solid",
      };
    }
  }, [messageType]);
  const finalContent = useMemo(() => {
    if (messageType === "code") {
      try {
        // Attempt to format the code as JSON, then parse the JSON into
        // a well-formatted string. The data may have values which are not well-formatted strings,
        // so we should clean up the line breaks.
        let formattedContent: string[]; // Formatted content is an array of lines
        let data: {
          [label: string]:
            | string
            | {
                [label: string]: string;
              };
        } = JSON.parse(content);
        formattedContent = Object.entries(data).map(([key, value]) => {
          if (typeof value === "object") {
            return [
              key,
              Object.entries(value)
                .map(([key, value]) => {
                  return [key.toUpperCase(), value].join("\n");
                })
                .join("\n"),
            ].join("\n");
          }
          return [key.toUpperCase(), value].join("\n");
        });
        return formattedContent.join("\n");
      } catch (error) {
        return content;
      }
    }
    return content;
  }, [messageType]);
  const messageColor = useMemo(() => {
    if (senderType === "You") {
      return theme.palette.secondary.main;
    }
    if (messageType) {
      switch (messageType) {
        case "text":
          return theme.palette.tertiary.main;
        case "code":
          return theme.palette.code.main;
        case "error":
          return theme.palette.error.main;
        default:
          return theme.palette.error.main;
      }
    }
  }, [messageType, theme.palette]);
  const [copiedMessage, setCopiedMessage] = useState<string>("");

  const copyToClipboard = () => {
    navigator.clipboard.writeText(finalContent);
    setCopiedMessage("Copied to clipboard!");
  };

  return (
    <Paper
      elevation={8}
      square={messageType === "code"}
      onClick={messageType === "code" ? copyToClipboard : undefined}
      sx={{
        width: "fit-content",
        background: messageColor,
        ...messageAttrs,
      }}
    >
      <Notification
        isOpen={copiedMessage !== ""}
        onClose={() => setCopiedMessage("")}
        severity="success"
        message={copiedMessage}
      />
      <Stack direction={"column"} sx={{ padding: "8px" }} spacing={1}>
        {finalContent.split("\n").map((line, index) => (
          <Typography variant="body1" align={justification} key={index}>
            {line}
          </Typography>
        ))}
      </Stack>
    </Paper>
  );
};

// ChatMessage Component renders a single message in the chat view
// based on the attributes of the message.
const ChatMessage: FC<{
  message: Message;
}> = ({ message }) => {
  const senderName = message.sender;
  const justification = (
    senderName === "You" ? "right" : "left"
  ) as Justification;

  const ChatMessageSections = useMemo(() => {
    return message.sections.map((section, index) => {
      return (
        <ChatMessageSection
          key={index}
          senderType={message.sender}
          messageType={section.messageType}
          content={section.content}
          justification={justification}
        />
      );
    });
  }, [message]);

  return (
    <ListItem>
      <Box
        sx={{
          display: "flex",
          width: "100%",
          justifyContent: justification,
        }}
      >
        <Stack direction={"column"} spacing={1}>
          <Typography variant="caption" align={justification}>
            {senderName}
          </Typography>
          {ChatMessageSections}
        </Stack>
      </Box>
    </ListItem>
  );
};

const ChatView: FC = () => {
  const { serverUrl, networkSettings, languageSettings } = useConfiguration();
  const [chatHistory, setChatHistory] = useState<Message[]>(
    defaultMessages.messages as Message[]
  );
  const [errorMessage, setErrorMessage] = useState<string>("");

  const addMessages = (messages: Message[]) => {
    setChatHistory(chatHistory.concat(messages));
  };

  const handleSendNewMessage = async (message: Message) => {
    if (networkSettings === undefined || languageSettings === undefined) {
      setErrorMessage("Please configure the network and language settings.");
      return;
    }
    const botMessage = sendMessage(serverUrl, {
      messageHistory: [...chatHistory, message],
      connectionParameters: networkSettings,
      languageSettings: languageSettings,
    }).catch((error) => {
      console.log(error);
      if (axios.isAxiosError(error)) {
        setErrorMessage(error.message);
      }
      return undefined;
    });
    return botMessage;
  };

  const MessageHistory = useMemo(() => {
    return chatHistory.map((message: Message, index) => (
      <ChatMessage key={index} message={message} />
    ));
  }, [chatHistory]);

  return (
    <Container
      maxWidth="xl"
      sx={{
        width: "100%",
        height: "100%",
        padding: "12px",
        overflow: "hidden",
      }}
    >
      <Notification
        isOpen={errorMessage !== ""}
        onClose={() => setErrorMessage("")}
        severity="error"
        message={errorMessage}
      />
      <Stack direction={"column"} spacing={1} height={"100%"}>
        <Paper
          elevation={2}
          sx={{
            flexGrow: 1,
            overflow: "scroll",
          }}
        >
          <List>{MessageHistory}</List>
        </Paper>
        <InputBox
          onClearMessages={() => setChatHistory([])}
          onSendMessage={async (message) => {
            addMessages([message]);
            let botMessage = await handleSendNewMessage(message);
            if (botMessage !== undefined) {
              addMessages([message, botMessage]);
            }
          }}
          onInternalMessage={(m) => {
            addMessages([m]);
          }}
        />
      </Stack>
    </Container>
  );
};

export default ChatView;
