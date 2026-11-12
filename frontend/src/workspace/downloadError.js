export const getDraftDownloadErrorMessage = async (error) => {
  const fallback = 'Error downloading draft. Please try again.';
  let responseData = error?.response?.data;

  if (typeof Blob !== 'undefined' && responseData instanceof Blob) {
    try {
      responseData = await responseData.text();
    } catch {
      return fallback;
    }
  }

  if (typeof responseData === 'string') {
    try {
      responseData = JSON.parse(responseData);
    } catch {
      return responseData.trim() || fallback;
    }
  }

  return responseData?.error_message
    || responseData?.message
    || responseData?.msg
    || responseData?.error
    || fallback;
};
