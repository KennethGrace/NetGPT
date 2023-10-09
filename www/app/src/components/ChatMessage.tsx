import React, { FC, useEffect, useMemo, useState } from "react";

import { Box, Paper, Slide, Stack, Typography, useTheme } from "@mui/material";

import {
  BotMessage,
  Message,
  MessageType,
  SenderType,
} from "../server/messaging";

type Justification = "right" | "left";

const TextMessage: FC<{
  content: string;
  color?: string;
}> = ({ content, color }) => {
  const theme = useTheme();

  return (
    <Paper
      elevation={8}
      sx={{
        width: "fit-content",
        maxWidth: "60vw",
        background: color ?? theme.palette.secondary.main,
      }}
    >
      <Stack direction={"column"} sx={{ padding: "8px" }} spacing={1}>
        {content.split("\n").map((line, index) => (
          <Typography variant="body1" align={"left"} key={index}>
            {line}
          </Typography>
        ))}
      </Stack>
    </Paper>
  );
};

const CodeMessage: FC<{
  content: string;
}> = ({ content }) => {
  const theme = useTheme();

  const copyToClipboard = () => {
    navigator.clipboard.writeText(content).then();
    setCopiedMessage("Copied to clipboard!");
  };

  const [copiedMessage, setCopiedMessage] = useState<string>("");

  return (
    <Paper
      elevation={8}
      square
      sx={{
        width: "fit-content",
        maxWidth: "60vw",
        background: theme.palette.code.main,
        maxHeight: "20vh",
        overflow: "auto",
        border: "1px solid",
      }}
    >
      <Stack direction={"column"} sx={{ padding: "8px" }} spacing={1}>
        {content.split("\n").map((line, index) => (
          <Typography variant="body1" align={"left"} key={index}>
            {line}
          </Typography>
        ))}
      </Stack>
    </Paper>
  );
};

const ChatMessageSection: FC<{
  senderType: SenderType;
  messageType: MessageType;
  content: string;
  caption?: string;
  justification: Justification;
}> = ({ senderType, messageType, content, caption, justification }) => {
  const theme = useTheme();
  const [showPaper, setShowPaper] = useState<boolean>(false);

  useEffect(() => {
    setShowPaper(true);
  }, []);

  const Content = useMemo(() => {
    switch (messageType) {
      case "text":
        const color =
          senderType === "You" ? theme.palette.primary.main : undefined;
        return <TextMessage content={content} color={color} />;
      case "code":
        return <CodeMessage content={content} />;
      default:
        return null;
    }
  }, [messageType, content, justification]);

  return (
    <Slide in={showPaper} direction={justification} mountOnEnter unmountOnExit>
      <Box
        sx={{
          display: "flex",
          width: "100%",
          justifyContent: justification,
        }}
      >
        <Stack direction={"column"} spacing={1}>
          {Content}
          {caption && (
            <Typography variant="caption" align={justification}>
              {caption}
            </Typography>
          )}
        </Stack>
      </Box>
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
