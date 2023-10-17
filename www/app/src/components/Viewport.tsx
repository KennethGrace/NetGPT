import React, { FC, lazy, useMemo, useState } from "react";

import {
  Container,
  Divider,
  LinearProgress,
  Paper,
  Stack,
  Typography,
} from "@mui/material";

import { Notification } from "../common/Notify";

import { useConfiguration } from "../context/configuration";
import { Message, sendMessage } from "../server/messaging";

import defaultMessages from "../json/defaultMessages.json";
import { useAuthentication } from "../context/authentication";
import { AuthenticationHandler } from "../server/authenticating";

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

  const Platform = useMemo(() => {
    if (networkSettings?.deviceType) {
      return (
        <ConfigBadge label="Platform" value={networkSettings.deviceType} />
      );
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
      {Platform}
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
  const { authServer } = useAuthentication();
  const [chatHistory, setChatHistory] = useState<Message[]>(
    defaultMessages.messages as Message[],
  );
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const addMessages = (messages: Message[]) => {
    setChatHistory(chatHistory.concat(messages));
  };

  const handleSendNewMessage = async (message: Message) => {
    if (
      authServer === undefined ||
      serverUrl === undefined ||
      networkSettings === undefined ||
      languageSettings === undefined
    ) {
      setErrorMessage("Please configure the server settings.");
      return undefined;
    }
    const token = await AuthenticationHandler.getToken();
    if (token) {
      return await sendMessage(
        serverUrl,
        {
          message_history: [...chatHistory, message],
          network_settings: networkSettings,
          language_settings: languageSettings,
        },
        token,
      ).catch((error) => {
        setErrorMessage(error);
        console.log(error);
        return undefined;
      });
    } else {
      setErrorMessage("Please log in to the server.");
      return undefined;
    }
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
        isOpen={errorMessage !== null}
        onClose={() => setErrorMessage(null)}
        severity="error"
        message={errorMessage ?? "An unknown error occurred."}
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
          onSendMessage={(message) => {
            addMessages([message]);
            setWaitingForResponse(true);
            handleSendNewMessage(message)
              .catch((error) => {
                setErrorMessage(error);
                return undefined;
              })
              .then((response) => {
                if (response) {
                  addMessages([message, response]);
                }
              })
              .finally(() => {
                setWaitingForResponse(false);
              });
          }}
        />
      </Stack>
    </Container>
  );
};

export default Viewport;
