import React, { FC, ReactNode, useMemo, useState } from "react";

import {
  Box,
  Button,
  Divider,
  IconButton,
  Paper,
  Stack,
  TextField,
} from "@mui/material";

import { Clear, Refresh, Send } from "@mui/icons-material";

import { useConfiguration } from "../context/configuration";
import { Message } from "../server/messaging";
import { useAuthentication } from "../context/authentication";

const NewMessage: (text: string) => Message = (text) => ({
  sender: "You",
  sections: [
    {
      messageType: "text",
      content: text,
    },
  ],
  // Time since the epoch in milliseconds (UTC)
  timestamp: new Date().getTime(),
});

const InputBoxButton: FC<{
  text: string;
  onClick: () => void;
  Icon: ReactNode;
  disabled?: boolean;
}> = ({ text, onClick, Icon, disabled }) => (
  <Button
    variant="contained"
    aria-label={text}
    onClick={onClick}
    color="inherit"
    endIcon={Icon}
    disabled={disabled}
  >
    {text}
  </Button>
);

interface InputBoxProps {
  onClearMessages: () => void;
  onSendMessage: (message: Message) => void;
}

// InputBox Component accepts input from the user and implements
// the logic for submitting a message to the chat.
const InputBox: FC<InputBoxProps> = ({ onClearMessages, onSendMessage }) => {
  const { languageSettings, networkSettings } = useConfiguration();
  const { isAuthenticated } = useAuthentication();
  const [inputText, setInputText] = useState<string>("");

  // useMemo keeps the value of isConfigured cached until the dependencies change.
  // This prevents the value from being recalculated on every render.
  const isConfigured = useMemo(() => {
    return (
      languageSettings !== undefined &&
      networkSettings !== undefined &&
      isAuthenticated
    );
  }, [languageSettings, networkSettings, isAuthenticated]);

  const sendMessage = (auto: boolean) => {
    // Validate the input text is not empty or all whitespace.
    if (inputText.length === 0 || !inputText.trim()) {
      return;
    }
    const message: Message = NewMessage(inputText);
    onSendMessage(message);
    setInputText("");
  };

  const handleKeyDown: React.KeyboardEventHandler<HTMLDivElement> = (event) => {
    if (event.key === "Enter") {
      // If the user pressed the 'Return' key.
      if (!event.shiftKey && inputText.length > 0) {
        // If the shift key is not pressed and the input is not empty, send the message.
        event.preventDefault();
        sendMessage(true);
      }
    }
  };

  return (
    <Paper
      elevation={4}
      sx={{
        display: "flex",
        width: "100%",
        height: "fit-content",
        padding: 2,
      }}
    >
      <Stack
        direction={"column"}
        spacing={1}
        sx={{
          flexGrow: 1,
        }}
      >
        <Stack direction={"row"} spacing={1}>
          <TextField
            id="outlined-basic"
            label="Message"
            variant="filled"
            multiline
            fullWidth
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            onKeyDown={handleKeyDown}
            sx={{
              overflow: "auto",
              wordWrap: "break-word",
            }}
            disabled={!isConfigured}
          />
          <Box>
            <IconButton
              aria-label="send message"
              size="large"
              onClick={() => sendMessage(false)}
              disabled={!isConfigured}
            >
              <Send />
            </IconButton>
          </Box>
        </Stack>
        <Stack direction={"row"} spacing={1}>
          <InputBoxButton
            text="New Chat"
            onClick={onClearMessages}
            Icon={<Refresh />}
            disabled={!isConfigured}
          />
          <Divider
            orientation="vertical"
            sx={{
              flexGrow: 1,
            }}
          />
          <InputBoxButton
            text="Clear"
            onClick={() => setInputText("")}
            Icon={<Clear />}
            disabled={!isConfigured}
          />
        </Stack>
      </Stack>
    </Paper>
  );
};

export default InputBox;
