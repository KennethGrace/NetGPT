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
  Divider,
  LinearProgress,
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
const ChatMessage = lazy(() => import("./ChatMessage"));

const ConfigBadge: FC<{ label: string; value: string }> = ({
  label,
  value,
}) => {
  return (
    <Stack direction={"row"} spacing={1} alignItems={"center"}>
      <Typography variant="caption" fontWeight="bold" noWrap>
        {label}
      </Typography>
      <Paper
        elevation={4}
        sx={{
          pl: 1,
          pr: 1,
          width: "fit-content",
        }}
      >
        <Typography variant="caption" noWrap>
          {value}
        </Typography>
      </Paper>
    </Stack>
  );
};

const ViewportHeader: FC<{
  showLoading?: boolean;
}> = ({ showLoading = false }) => {
  const { networkSettings, languageSettings } = useConfiguration();

  const DeviceType = useMemo(() => {
    if (networkSettings?.deviceType) {
      return <ConfigBadge label="Device" value={networkSettings.deviceType} />;
    }
    return null;
  }, [networkSettings]);

  const Language = useMemo(() => {
    if (languageSettings?.name) {
      return <ConfigBadge label="Language" value={languageSettings.name} />;
    }
    return null;
  }, [languageSettings]);

  const visibility = useMemo(() => {
    if (showLoading) {
      return "visible";
    }
    return "hidden";
  }, [showLoading]);

  return (
    <Stack
      direction={"row"}
      justifyContent={"space-between"}
      alignItems={"center"}
      spacing={1}
    >
      {DeviceType}
      <Divider orientation="vertical" flexItem />
      <LinearProgress
        sx={{
          flexGrow: 1,
          visibility: visibility,
          pr: 1,
          pl: 1,
        }}
      />
      <Divider orientation="vertical" flexItem />
      {Language}
    </Stack>
  );
};

const Viewport: FC = () => {
  const [waitingForResponse, setWaitingForResponse] = useState<boolean>(false);
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
        <ViewportHeader showLoading={waitingForResponse} />
        <Paper
          elevation={2}
          sx={{
            flexGrow: 1,
            overflow: "auto",
            overflowX: "hidden",
            padding: "16px",
          }}
        >
          <Stack direction={"column"} spacing={1}>
            {MessageHistory}
          </Stack>
        </Paper>
        <InputBox
          onClearMessages={() => setChatHistory([])}
          onSendMessage={async (message) => {
            addMessages([message]);
            setWaitingForResponse(true);
            let botMessage = await handleSendNewMessage(message);
            if (botMessage !== undefined) {
              addMessages([message, botMessage]);
            }
            setWaitingForResponse(false);
          }}
          onInternalMessage={(m) => {
            addMessages([m]);
          }}
        />
      </Stack>
    </Container>
  );
};

export default Viewport;
