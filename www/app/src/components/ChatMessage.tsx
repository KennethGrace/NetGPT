import React, { FC, useEffect, useMemo, useState } from "react";

import {
  Box,
  ListItem,
  Typography,
  Paper,
  Stack,
  useTheme,
  Slide,
} from "@mui/material";

import {
  Message,
  BotMessage,
  MessageType,
  SenderType,
} from "../server/messaging";

import { Notification } from "../common/Notify";

type Justification = "right" | "left";

const ChatMessageSection: FC<{
  senderType: SenderType;
  messageType: MessageType;
  content: string;
  caption?: string;
  justification: Justification;
}> = ({ senderType, messageType, content, caption, justification }) => {
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
    if (messageType === "text") {
      return content;
    }
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
  const [showPaper, setShowPaper] = useState<boolean>(false);

  const copyToClipboard = () => {
    navigator.clipboard.writeText(finalContent);
    setCopiedMessage("Copied to clipboard!");
  };

  useEffect(() => {
    setShowPaper(true);
  }, []);

  return (
    <Slide in={showPaper} direction={justification} mountOnEnter unmountOnExit>
      <Paper
        elevation={8}
        square={messageType === "code"}
        onClick={messageType === "code" ? copyToClipboard : undefined}
        sx={{
          width: "fit-content",
          background: messageColor,
          zIndex: 1,
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
    </Slide>
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
  const caption = useMemo(() => {
    try {
      return (message as BotMessage).caption;
    } catch (error) {
      return undefined;
    }
  }, [message]);

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
    <Box
      sx={{
        display: "flex",
        width: "100%",
        justifyContent: justification,
      }}
    >
      <Stack direction={"column"} spacing={1}>
        <Typography variant="caption" align={justification} fontSize={14}>
          {senderName}
        </Typography>
        {ChatMessageSections}
        {caption && (
          <Typography variant="caption" align={justification}>
            {caption}
          </Typography>
        )}
      </Stack>
    </Box>
  );
};

export default ChatMessage;
