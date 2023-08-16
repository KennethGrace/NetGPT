import React, { FC, useEffect, useMemo, useState } from "react";

import {
  Box,
  Typography,
  MenuItem,
  TextField,
  Stack,
  Accordion,
  AccordionSummary,
  AccordionDetails,
  Tooltip,
} from "@mui/material";
import { LanguageSettings, useConfiguration } from "../../common/configuration";

export interface LanguageFieldProps {
  title: string;
  languages: LanguageSettings[];
  setSelection: (selection: LanguageSettings) => void;
  disabled?: boolean;
}

/**
 * The LanguageField component
 *
 * This is a surface for the user to enter configuration
 * information based on a dynamic set of fields. The component is an Accordion
 * surface embedded in a Dialog surface. The AccordionSummary holds a Selection
 * TextField for the user to select the language they wish to configure. The
 * AccordionDetails then holds the dynamic set of fields for the user to enter
 * the configuration information for the selected language.
 */
export const LanguageField: FC<LanguageFieldProps> = ({
  title,
  languages,
  setSelection,
  disabled,
}) => {
  const { name, description, fields } = useConfiguration().languageSettings ?? {
    name: "",
    fields: {},
  };
  // Form State
  const [languageChoice, setLanguageChoice] = useState<string>(name);
  const [fieldValues, setFieldValues] = useState<Record<string, string>>(fields);

  const selectedLanguage = useMemo(() => {
    return languages.find((language) => language.name === languageChoice);
  }, [languageChoice]);

  // Use an effect to update the Language Settings on state change
  useEffect(() => {
    setSelection({
      name: languageChoice,
      description: selectedLanguage?.description ?? "",
      fields: fieldValues,
    });
  }, [languageChoice, fieldValues]);

  // Display State
  const [fieldComponents, setFieldComponents] = useState<JSX.Element[]>([]);

  // Update the fieldComponents based on the selected language
  useEffect(() => {
    const fields = Object.entries(selectedLanguage?.fields ?? {}).map(([label, _]) => {
      return (
        <TextField
          key={label}
          margin="dense"
          id={label}
          label={label}
          type="text"
          fullWidth
          variant="filled"
          onChange={(event) => {
            setFieldValues((prev) => {
              prev[label] = event.target.value;
              return {...prev};
            });
          }}
        />
      );
    });
    setFieldComponents(fields);
  }, [languageChoice]);

  return (
    <Accordion elevation={3} expanded={languageChoice !== ""} disableGutters>
      <AccordionSummary>
        <Stack
          direction="column"
          spacing={1}
          sx={{
            width: "100%",
          }}
        >
          <Typography variant="overline">{title}</Typography>
          <TextField
            fullWidth
            select
            type="text"
            margin="dense"
            defaultValue={name}
            label="Language Settings"
            onChange={(e) => {
              setLanguageChoice(e.target.value);
            }}
            variant="filled"
          >
            {languages.map((language, index) => {
              return (
                <MenuItem key={index} value={language.name}>
                  <Tooltip
                    arrow
                    title={language.description}
                    key={language.name}
                  >
                    <Box flexGrow={1}>{language.name}</Box>
                  </Tooltip>
                </MenuItem>
              );
            })}
          </TextField>
        </Stack>
      </AccordionSummary>
      <AccordionDetails>
        <Stack direction="column" spacing={1}>
          {fieldComponents}
        </Stack>
      </AccordionDetails>
    </Accordion>
  );
};

export default LanguageField;
